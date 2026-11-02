"""Parse a pasted list of profile URLs into (platform, handle) pairs and validate handles."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, unquote

PLATFORMS = ["instagram", "x", "tiktok", "youtube", "threads", "facebook"]

PLATFORM_LABEL = {
    "instagram": "Instagram",
    "x": "X",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "threads": "Threads",
    "facebook": "Facebook",
}

_HOSTS = {
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "threads.com": "threads",
    "threads.net": "threads",
    "facebook.com": "facebook",
    "fb.com": "facebook",
}

# Username rules of each platform. A handle that breaks them cannot exist there.
_RULES = {
    "instagram": (re.compile(r"^[A-Za-z0-9._]{1,30}$"),
                  "Instagram usernames allow only letters, numbers, dots and underscores (max 30)"),
    "threads": (re.compile(r"^[A-Za-z0-9._]{1,30}$"),
                "Threads usernames allow only letters, numbers, dots and underscores (max 30)"),
    "x": (re.compile(r"^[A-Za-z0-9_]{1,15}$"),
          "X usernames are max 15 characters: letters, numbers and underscores only (no dots)"),
    "tiktok": (re.compile(r"^[A-Za-z0-9._]{2,24}$"),
               "TikTok usernames allow only letters, numbers, dots and underscores (max 24)"),
    "youtube": (re.compile(r"^[A-Za-z0-9._-]{3,30}$"),
                "YouTube handles are 3-30 characters: letters, numbers, dots, underscores, hyphens"),
    "facebook": (re.compile(r"^[A-Za-z0-9.]{5,50}$"),
                 "Facebook usernames allow only letters, numbers and dots (no underscores, min 5)"),
}


@dataclass
class Account:
    platform: str
    handle: str
    url: str
    problem: str = ""          # non-empty => handle cannot exist on this platform
    label: str = ""            # optional group name ("URL | Group")
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.platform, self.handle.lower())


def _platform_of(host: str) -> str | None:
    host = host.lower()
    for suffix, plat in _HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return plat
    return None


def profile_url(platform: str, handle: str) -> str:
    return {
        "instagram": f"https://www.instagram.com/{handle}/",
        "x": f"https://x.com/{handle}",
        "tiktok": f"https://www.tiktok.com/@{handle}",
        "youtube": f"https://www.youtube.com/@{handle}",
        "threads": f"https://www.threads.com/@{handle}",
        "facebook": f"https://www.facebook.com/{handle}",
    }[platform]


def parse_line(line: str) -> Account | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    label = ""
    if "|" in line:  # "URL | Group"
        line, label = (x.strip() for x in line.split("|", 1))
    acc = _parse_url(line)
    if acc:
        acc.label = label
    return acc


def _parse_url(line: str) -> Account | None:
    if "://" not in line:
        line = "https://" + line
    try:
        u = urlparse(line)
        u.port                     # raises for malformed hosts like "x.com:abc" or "[..."
    except ValueError:
        return None                # a malformed line is reported as "not a profile URL", not a crash
    plat = _platform_of(u.netloc)
    if not plat:
        return None
    parts = [unquote(p) for p in u.path.split("/") if p]
    if not parts:
        return None

    handle = ""
    if plat == "facebook" and parts[0] == "profile.php":
        m = re.search(r"(?:^|&)id=(\d+)", u.query)
        if m:
            acc = Account(plat, m.group(1), f"https://www.facebook.com/profile.php?id={m.group(1)}")
            return acc
        return None
    if plat == "facebook" and parts[0] in ("p", "people") and len(parts) > 1:
        # facebook.com/p/Name-1000123..., facebook.com/people/Name/1000123...
        m = re.search(r"(\d{6,})$", parts[-1])
        if m:
            return Account(plat, m.group(1), f"https://www.facebook.com/profile.php?id={m.group(1)}")
        return None
    if plat == "youtube" and parts[0] in ("channel", "c", "user") and len(parts) > 1:
        acc = Account(plat, parts[1], f"https://www.youtube.com/{parts[0]}/{parts[1]}")
        return acc
    handle = parts[0].lstrip("@")
    if not handle:
        return None

    acc = Account(plat, handle, profile_url(plat, handle))
    rule, msg = _RULES[plat]
    if not rule.match(handle):
        acc.problem = msg
    return acc


def parse_accounts(text: str, platforms: list[str] | None = None) -> tuple[list[Account], list[str]]:
    """Return (unique accounts in input order, unrecognised lines)."""
    seen: set[tuple[str, str]] = set()
    out: list[Account] = []
    bad: list[str] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        acc = parse_line(raw)
        if acc is None:
            bad.append(raw.strip())
            continue
        if platforms and acc.platform not in platforms:
            continue
        if acc.key in seen:
            continue
        seen.add(acc.key)
        out.append(acc)
    return out, bad
