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


def migrate(log=print) -> None:
    """Run once at start-up. Never overwrites anything that already exists in the new place."""
    for d in (APP_DATA, LISTS_DIR, OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)
    if not _has_profile(PROFILE_DIR):
        for folder in _old_app_folders():
            old = os.path.join(folder, "browser_profile")
            if _has_profile(old):
                try:
                    shutil.copytree(old, PROFILE_DIR, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns("Singleton*", "*.lock", "Cache", "Code Cache", "GPUCache"))
                    log(f"Logins copied from {old} — you do not need to log in again.")
                except (OSError, shutil.Error):
                    pass
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
