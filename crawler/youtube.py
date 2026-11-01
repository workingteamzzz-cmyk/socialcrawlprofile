"""YouTube: list a channel's Videos / Shorts / Live tabs with yt-dlp (no API key, no login)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .accounts import Account
from .browser import Post, Result, Stopped

TABS = [("videos", "watch"), ("shorts", "shorts"), ("streams", "watch")]


def _quiet_logger():
    class L:
        def debug(self, msg): pass
        def info(self, msg): pass
        def warning(self, msg): pass
        def error(self, msg): pass
    return L()


def _video_time(ydl, vid: str) -> datetime | None:
    info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
    if info.get("live_status") == "is_upcoming":
        return None
    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    d = info.get("upload_date")
    if d:
        return datetime.strptime(d, "%Y%m%d").replace(hour=12, tzinfo=timezone.utc)
    return None


def crawl_youtube(acc: Account, start: datetime, end: datetime, log: Callable[[str], None], stop_flag) -> Result:
    import yt_dlp

    opts = {
        "quiet": True, "no_warnings": True, "logger": _quiet_logger(),
        "extract_flat": "in_playlist", "lazy_playlist": True,
        "skip_download": True, "ignore_no_formats_error": True,
        "socket_timeout": 30, "retries": 3, "extractor_retries": 3,
    }
    base = acc.url.rstrip("/")
    posts: list[Post] = []
    seen: set[str] = set()
    any_tab = False
    with yt_dlp.YoutubeDL(opts) as ydl:
        for tab, kind in TABS:
            if stop_flag.is_set():
                raise Stopped()
            try:
                listing = ydl.extract_info(f"{base}/{tab}", download=False, process=False)
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if "does not have" in msg or "no " + tab in msg:
                    continue
                if "404" in msg or "does not exist" in msg or "not found" in msg:
                    if not any_tab:
                        return Result("not_found", [], "Channel not found")
                    continue
                log(f"   ⚠ tab {tab}: {str(e).splitlines()[0][:150]}")
                continue
            any_tab = True
            older_streak = 0
            for entry in listing.get("entries") or []:
                if stop_flag.is_set():
                    raise Stopped()
                vid = (entry or {}).get("id")
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                try:
                    t = _video_time(ydl, vid)
                except Exception as e:  # noqa: BLE001
                    log(f"   ⚠ video {vid}: {str(e).splitlines()[0][:120]}")
                    continue
                if t is None:
                    continue
                if t < start:
                    older_streak += 1
                    if older_streak >= 2:   # tabs are newest-first
                        break
                    continue
                older_streak = 0
                if t <= end:
                    url = f"https://www.youtube.com/shorts/{vid}" if kind == "shorts" \
                        else f"https://www.youtube.com/watch?v={vid}"
                    posts.append(Post(url, t))
    if not any_tab:
        return Result("error", [], "Channel could not be read")
    return Result("ok", posts)


def search_youtube(query: str, start: datetime, end: datetime, cap: int, log: Callable[[str], None], stop_flag) -> Result:
    """YouTube search sorted by upload date (newest first); stops once results pass the start date."""
    import yt_dlp

    opts = {
        "quiet": True, "no_warnings": True, "logger": _quiet_logger(),
        "extract_flat": "in_playlist", "lazy_playlist": True,
        "skip_download": True, "ignore_no_formats_error": True,
        "socket_timeout": 30, "retries": 3, "extractor_retries": 3,
    }
    limit = cap * 2 if cap else 1500
    posts: list[Post] = []
    older_streak = 0
    outside = 0
    with yt_dlp.YoutubeDL(opts) as ydl:
        listing = ydl.extract_info(f"ytsearchdate{limit}:{query}", download=False, process=False)
        for entry in listing.get("entries") or []:
            if stop_flag.is_set():
                raise Stopped()
            vid = (entry or {}).get("id")
            if not vid:
                continue
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
            except Exception as e:  # noqa: BLE001
                log(f"   ⚠ video {vid}: {str(e).splitlines()[0][:120]}")
                continue
            if info.get("live_status") == "is_upcoming":
                continue
            ts = info.get("timestamp") or info.get("release_timestamp")
            if ts:
                t = datetime.fromtimestamp(ts, tz=timezone.utc)
            elif info.get("upload_date"):
                t = datetime.strptime(info["upload_date"], "%Y%m%d").replace(hour=12, tzinfo=timezone.utc)
            else:
                continue
            if t < start:
                older_streak += 1
                outside += 1
                if older_streak >= 3:
                    break
                continue
            older_streak = 0
            if t > end:
                outside += 1
                continue
            url = entry.get("url") or ""
            url = f"https://www.youtube.com/shorts/{vid}" if "/shorts/" in url else f"https://www.youtube.com/watch?v={vid}"
            author = info.get("uploader_id") or info.get("channel") or info.get("uploader") or ""
            posts.append(Post(url, t, author))
            if cap and len(posts) >= cap:
                break
    note = f"query: {query}" + (f"; {outside} results outside the period dropped" if outside else "")
    return Result("ok", posts, note)
