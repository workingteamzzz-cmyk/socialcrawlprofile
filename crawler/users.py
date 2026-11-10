"""Accounts for the server dashboard: username + password, one of three roles.

  viewer    sees results, schedules and the account lists; downloads Excel files
  operator  + runs searches, schedules, account lists, the file fixer, solves verifications
  admin     + manages users, imports platform cookies, sees the audit log

Passwords are stored only as scrypt hashes (salted). Each user may also have a personal API key (for scripts/n8n)
with the same role; only its SHA-256 is stored and it is shown once. Changing a password, disabling a user or
deleting them ends their dashboard sessions at once (session version). The keys in COLLECTOR_API_KEYS keep working as
admin keys — they are how the first admin signs in to create the accounts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

ROLES = ("viewer", "operator", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MIN_PASSWORD = 10


class UserError(ValueError):
    pass


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        _algo, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, TypeError):
        return False


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def allowed(role: str, needed: str) -> bool:
    return RANK.get(role, -1) >= RANK[needed]


class Users:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self._mtime = 0.0
        self.data: dict[str, dict] = self._load()
        self.secret = self._secret()
        self.fails: dict[str, list[float]] = {}            # username -> recent failed sign-ins

    # ---------- storage ----------
    def _load(self) -> dict:
        try:
            self._mtime = os.path.getmtime(self.path)
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _fresh(self):
        """Pick up accounts added from the command line (serve.py add-user) while the server runs."""
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return
        if m != self._mtime:
            self.data = self._load()

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._mtime = os.path.getmtime(self.path)

    def _secret(self) -> bytes:
        p = os.path.join(os.path.dirname(self.path) or ".", "session.secret")
        try:
            with open(p, "rb") as f:
                s = f.read()
            if len(s) >= 32:
                return s
        except OSError:
            pass
        s = secrets.token_bytes(32)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "wb") as f:
            f.write(s)
        os.chmod(p, 0o600)
        return s

    # ---------- admin actions ----------
    def list(self) -> list[dict]:
        with self.lock:
            self._fresh()
            return [{"username": n, "role": u["role"], "disabled": bool(u.get("disabled")),
                     "created": u.get("created"), "last_login": u.get("last_login"),
                     "has_api_key": bool(u.get("api_key"))} for n, u in sorted(self.data.items())]

    def save(self, username: str, role: str, password: str = "", disabled: bool = False, by: str = "") -> dict:
        name = (username or "").strip().lower()
        if not NAME_RE.match(name):
            raise UserError("Username: 2–32 lowercase letters, digits, dot, - or _ (e.g. ana or budi.s).")
        if role not in ROLES:
            raise UserError("Choose a role: viewer, operator or admin.")
        with self.lock:
            self._fresh()
            u = self.data.get(name)
            if u is None:
                if len(password or "") < MIN_PASSWORD:
                    raise UserError(f"Give a password of at least {MIN_PASSWORD} characters.")
                u = {"created": time.strftime("%Y-%m-%d %H:%M"), "ver": 1}
                self.data[name] = u
            if name == by and (role != "admin" or disabled):
                raise UserError("You cannot remove your own admin rights or disable yourself.")
            if u.get("role") == "admin" and (role != "admin" or disabled) and self._admins() <= 1:
                raise UserError("There must be at least one active admin.")
            changed = u.get("role") != role or bool(u.get("disabled")) != bool(disabled)
            u["role"], u["disabled"] = role, bool(disabled)
            if password:
                if len(password) < MIN_PASSWORD:
                    raise UserError(f"The password needs at least {MIN_PASSWORD} characters.")
                u["pw"] = _hash_password(password)
                changed = True
            if changed:
                u["ver"] = u.get("ver", 1) + 1            # ends this user's open sessions
            self._save()
            return {"username": name, "role": role, "disabled": bool(disabled)}

    def _admins(self) -> int:
        return sum(1 for u in self.data.values() if u.get("role") == "admin" and not u.get("disabled"))

    def delete(self, username: str, by: str = "") -> None:
        with self.lock:
            u = self.data.get(username)
            if not u:
                raise UserError("No such user.")
            if username == by:
                raise UserError("You cannot delete yourself.")
            if u.get("role") == "admin" and not u.get("disabled") and self._admins() <= 1:
                raise UserError("There must be at least one active admin.")
            del self.data[username]
            self._save()

    def new_api_key(self, username: str) -> str:
        with self.lock:
            u = self.data.get(username)
            if not u:
                raise UserError("No such user.")
            key = "plc_" + secrets.token_urlsafe(30)
            u["api_key"] = _key_hash(key)
            self._save()
            return key

    def change_password(self, username: str, old: str, new: str) -> None:
        with self.lock:
            u = self.data.get(username)
            if not u or not _check_password(old or "", u.get("pw", "")):
                raise UserError("The current password is not right.")
            if len(new or "") < MIN_PASSWORD:
                raise UserError(f"The new password needs at least {MIN_PASSWORD} characters.")
            u["pw"] = _hash_password(new)
            u["ver"] = u.get("ver", 1) + 1
            self._save()

    # ---------- signing in ----------
    def locked(self, username: str) -> bool:
        now = time.time()
        hits = [t for t in self.fails.get(username, []) if now - t < 900]
        self.fails[username] = hits
        return len(hits) >= 5

    def check_login(self, username: str, password: str) -> str | None:
        """The user's name when the password is right (and the account active), else None."""
        name = (username or "").strip().lower()
        if self.locked(name):
            return None
        with self.lock:
            self._fresh()
            u = self.data.get(name)
            ok = bool(u) and not u.get("disabled") and _check_password(password or "", u.get("pw", ""))
            if not u:
                _check_password(password or "", _hash_password("timing"))    # same time for unknown names
            if ok:
                u["last_login"] = time.strftime("%Y-%m-%d %H:%M")
                self._save()
        if not ok:
            self.fails.setdefault(name, []).append(time.time())
            return None
        self.fails.pop(name, None)
        return name

    def by_api_key(self, key: str) -> tuple[str, str] | None:
        if not key or not key.startswith("plc_"):
            return None
        h = _key_hash(key)
        with self.lock:
            self._fresh()
            for name, u in self.data.items():
                if not u.get("disabled") and u.get("api_key") and hmac.compare_digest(u["api_key"], h):
                    return name, u["role"]
        return None

    def session(self, username: str) -> str:
        with self.lock:
            ver = self.data[username].get("ver", 1)
        issued = int(time.time())
        sig = hmac.new(self.secret, f"u|{username}|{issued}|{ver}".encode(), hashlib.sha256).hexdigest()
        return f"u.{username}.{issued}.{sig}"

    def from_session(self, value: str, max_age_s: float) -> tuple[str, str] | None:
        try:
            tag, name, issued, sig = value.split(".", 3)
            issued_i = int(issued)
        except ValueError:
            return None
        if tag != "u" or time.time() - issued_i > max_age_s or issued_i > time.time() + 60:
            return None
        with self.lock:
            self._fresh()
            u = self.data.get(name)
            if not u or u.get("disabled"):
                return None
            want = hmac.new(self.secret, f"u|{name}|{issued_i}|{u.get('ver', 1)}".encode(), hashlib.sha256).hexdigest()
            return (name, u["role"]) if hmac.compare_digest(sig, want) else None
