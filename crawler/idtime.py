"""Read the publish time straight out of a post's ID, so posts never need to be opened.

- Instagram / Threads: the shortcode is the media ID in base64; its top bits are
  milliseconds since 2011-08-24 21:07:01.721 UTC.
- X: tweet IDs are Twitter snowflakes (ms since 2010-11-04 01:42:54.657 UTC, >> 22).
- TikTok: video/photo IDs carry the Unix second in their top 32 bits.
"""
from __future__ import annotations

from datetime import datetime, timezone

_IG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_IG_EPOCH_MS = 1314220021721
_X_EPOCH_MS = 1288834974657


def _utc_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def instagram_shortcode_time(code: str) -> datetime | None:
    # Codes longer than 11 chars (private/archived variants) carry the ID in the first 11.
    code = code[:11]
    media_id = 0
    for ch in code:
        idx = _IG_ALPHABET.find(ch)
        if idx < 0:
            return None
        media_id = media_id * 64 + idx
    ms = (media_id >> 23) + _IG_EPOCH_MS
    return _sane(_utc_ms(ms))


def x_status_time(status_id: str | int) -> datetime | None:
    try:
        sid = int(status_id)
    except (TypeError, ValueError):
        return None
    if sid < 30_000_000_000:   # pre-snowflake tweets (before Nov 2010)
        return None
    return _sane(_utc_ms((sid >> 22) + _X_EPOCH_MS))


def tiktok_id_time(video_id: str | int) -> datetime | None:
    try:
        vid = int(video_id)
    except (TypeError, ValueError):
        return None
    return _sane(datetime.fromtimestamp(vid >> 32, tz=timezone.utc))


def _sane(dt: datetime) -> datetime | None:
    if datetime(2010, 1, 1, tzinfo=timezone.utc) <= dt <= datetime(2100, 1, 1, tzinfo=timezone.utc):
        return dt
    return None
