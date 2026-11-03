"""Where the app keeps its data — outside the app folder, so a new version keeps logins, lists and results.

- Browser profile (logins):  Windows %LOCALAPPDATA%\\PostLinkCollector\\browser_profile
                             Mac     ~/Library/Application Support/PostLinkCollector/browser_profile
                             Linux   ~/.local/share/PostLinkCollector/browser_profile
- Account lists and results: Documents/Post Link Collector/lists and .../outputs  (easy to open in Excel)

CRAWLER_HOME (one folder for everything) or OUTPUT_DIR can override these.
On first start, logins and lists from an older version's folder (this folder, or a sibling folder such as
"post-link-collector (1)") are copied over once.
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLED_LISTS = os.path.join(ROOT, "data", "lists")


def _app_data() -> str:
    if os.environ.get("CRAWLER_HOME"):
        return os.environ["CRAWLER_HOME"]
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"), "PostLinkCollector")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/PostLinkCollector")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "PostLinkCollector")


def _documents() -> str:
    if os.environ.get("CRAWLER_HOME"):
        return os.environ["CRAWLER_HOME"]
    docs = os.path.expanduser("~/Documents")
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "Post Link Collector")


APP_DATA = _app_data()
PROFILE_DIR = os.path.join(APP_DATA, "browser_profile")
LOGINS_FILE = os.path.join(APP_DATA, "logins.json")
LISTS_DIR = os.path.join(_documents(), "lists")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR") or os.path.join(_documents(), "outputs")


def _old_app_folders() -> list[str]:
    """This folder and sibling folders that look like earlier copies of the app (newest first)."""
    out = [ROOT]
    parent = os.path.dirname(ROOT)
    try:
        for name in os.listdir(parent):
            p = os.path.join(parent, name)
            if p != ROOT and os.path.isfile(os.path.join(p, "run.py")) and os.path.isdir(os.path.join(p, "crawler")):
                out.append(p)
    except OSError:
        pass
    return sorted(out, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)


def _has_profile(d: str) -> bool:
    return os.path.isdir(d) and any(n not in ("SingletonLock", "SingletonCookie", "SingletonSocket") for n in os.listdir(d))


def _cookie_mtime(profile: str) -> float:
    """When the profile's cookie store was last written (0 if it has none) — i.e. when it was last used signed in."""
    best = 0.0
    for rel in (("Default", "Network", "Cookies"), ("Default", "Cookies")):
        p = os.path.join(profile, *rel)
        if os.path.isfile(p):
            best = max(best, os.path.getmtime(p))
    return best


def _signed_in_here() -> bool:
    import json
    try:
        with open(LOGINS_FILE, encoding="utf-8") as f:
            return any((json.load(f).get("platforms") or {}).values())
    except (OSError, ValueError, AttributeError):
        return False


def find_old_profiles() -> list[dict]:
    """Browser profiles of older copies of the app: next to this folder, and in Downloads/Desktop/Documents/home
    (two levels deep). Newest first: [{"path": profile_dir, "app": app_folder, "last_used": epoch}]."""
    roots = {os.path.dirname(ROOT)}
    home = os.path.expanduser("~")
    for name in ("Downloads", "Desktop", "Documents", ""):
        roots.add(os.path.join(home, name) if name else home)
    seen, out = set(), []

    def consider(folder):
        prof = os.path.join(folder, "browser_profile")
        real = os.path.realpath(prof)
        if real in seen or real == os.path.realpath(PROFILE_DIR) or not _has_profile(prof):
            return
        seen.add(real)
        out.append({"path": prof, "app": folder, "last_used": _cookie_mtime(prof)})
    for root in roots:
        try:
            level1 = [os.path.join(root, n) for n in os.listdir(root) if not n.startswith(".")]
        except OSError:
            continue
        for f1 in level1:
            if not os.path.isdir(f1):
                continue
            if os.path.isfile(os.path.join(f1, "run.py")):
                consider(f1)
                continue
            try:                                   # e.g. Downloads/post-link-collector-HEAD/post-link-collector
                for n in os.listdir(f1)[:200]:
                    f2 = os.path.join(f1, n)
                    if os.path.isdir(f2) and os.path.isfile(os.path.join(f2, "run.py")):
                        consider(f2)
            except OSError:
                pass
    return sorted(out, key=lambda x: x["last_used"], reverse=True)


def import_profile(src: str, log=print) -> str:
    """Copy logins from an older copy of the app (its folder or its browser_profile folder) into this app's profile.
    The current profile is kept as a backup. Returns "" on success or an error message."""
    src = os.path.expanduser((src or "").strip().strip('"'))
    if os.path.isdir(os.path.join(src, "browser_profile")):
        src = os.path.join(src, "browser_profile")
    if not _has_profile(src):
        return "No logins found there — choose the old app folder (the one with run.py) or its browser_profile folder."
    if os.path.realpath(src) == os.path.realpath(PROFILE_DIR):
        return "That is already the profile this app uses."
    if _has_profile(PROFILE_DIR):
        import time
        backup = PROFILE_DIR + time.strftime("_backup_%Y%m%d_%H%M%S")
        n = 1
        while os.path.exists(backup):
            backup = PROFILE_DIR + time.strftime("_backup_%Y%m%d_%H%M%S") + f"_{n}"
            n += 1
        try:
            os.replace(PROFILE_DIR, backup)
        except OSError as e:
            return f"Could not set the current profile aside ({e}). Close every window the app opened and try again."
    try:
        shutil.copytree(src, PROFILE_DIR, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("Singleton*", "*.lock", "lockfile", "Cache", "Code Cache", "GPUCache",
                                                      "Service Worker", "GrShaderCache", "ShaderCache"))
    except (OSError, shutil.Error) as e:
        return f"Copy failed: {e}"
    try:
        os.remove(LOGINS_FILE)                 # the next login/run checks the imported sessions again
    except OSError:
        pass
    log(f"Logins copied from {src} — you do not need to log in again.")
    return ""


def migrate(log=print) -> None:
    """Run at start-up. Copies logins from an older copy of the app when this one has not been signed in yet
    (or only has an older, unused profile) — the current profile is kept as a backup, never silently overwritten."""
    for d in (APP_DATA, LISTS_DIR, OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)
    if not _signed_in_here():
        mine = _cookie_mtime(PROFILE_DIR) if _has_profile(PROFILE_DIR) else -1
        for cand in find_old_profiles():
            if cand["last_used"] > mine and cand["last_used"] > 0:
                import_profile(cand["path"], log)
            break
    # Lists from older copies of the app and the bundled example — each copied only once, so a list the user
    # deleted does not come back.
    import json
    done_file = os.path.join(APP_DATA, "lists_copied.json")
    try:
        with open(done_file, encoding="utf-8") as f:
            done = set(json.load(f))
    except (OSError, ValueError):
        done = set()
    sources = [os.path.join(f, "data", "lists") for f in _old_app_folders()] + [BUNDLED_LISTS]
    for src in sources:
        if not os.path.isdir(src):
            continue
        for name in os.listdir(src):
            if not name.endswith(".csv") or name in done:
                continue
            done.add(name)
            if not os.path.exists(os.path.join(LISTS_DIR, name)):
                try:
                    shutil.copy2(os.path.join(src, name), os.path.join(LISTS_DIR, name))
                except OSError:
                    pass
    try:
        with open(done_file, "w", encoding="utf-8") as f:
            json.dump(sorted(done), f)
    except OSError:
        pass
