"""Publication times of sites that label their local time as UTC.

Many sites (often WordPress with the wrong time zone setting) write the article's local time — the one printed on
the page — with a UTC mark: <meta property="article:published_time" content="2026-11-21T07:00:35+00:00"> while the
page says "Sabtu, 21 November 2026 | 07:00:35". Converted from UTC to WIB that becomes 14:00 — 7 hours late.

The team's article reader (medmas) is used as is; this wraps its date detection: when the first timed tag carries a
UTC mark (Z / +00:00) and its clock time equals the time printed on the page (same day), the mark is wrong and the
printed time is used (WIB, or the WITA / WIT the page names). Anything else is left exactly as the reader decided.
"""
from __future__ import annotations

import re

_STATE = {"orig": None}
_UTC_RAW = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\s*(Z|[+-]00:?00)\b", re.I)


def _first_timed_candidate(soup) -> str:
    import json
    for sel in [{"property": "article:published_time"}, {"itemprop": "datePublished"}, {"name": "publishdate"},
                {"name": "pubdate"}, {"name": "DC.date.issued"}, {"name": "date"},
                {"property": "og:article:published_time"}]:
        tag = soup.find("meta", sel)
        if tag and tag.get("content") and re.search(r"\d{1,2}:\d{2}", tag["content"]):
            return tag["content"]
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:  # noqa: BLE001
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and re.search(r"\d{1,2}:\d{2}", str(item.get("datePublished") or "")):
                return str(item["datePublished"])
    return ""


def corrected(soup, d: str, t: str) -> tuple[str, str]:
    from .medmas import extract as ex
    raw = _UTC_RAW.search(_first_timed_candidate(soup) or "")
    if not raw:
        return d, t
    m = ex._TEXT_DT_RE.search(soup.get_text(" ", strip=True)[:4000])
    if not m:
        return d, t
    month = ex._MONTHS_ID.get(m.group(2).lower())
    same_day = month and (int(m.group(3)), month, int(m.group(1))) == (int(raw.group(1)), int(raw.group(2)),
                                                                        int(raw.group(3)))
    same_clock = (int(m.group(4)), int(m.group(5))) == (int(raw.group(4)), int(raw.group(5)))
    if same_day and same_clock:                         # local time marked as UTC: the page is right
        d2, t2 = ex._from_text_match(m)
        if d2 and t2:
            return d2, t2
    return d, t


def install() -> None:
    from .medmas import extract as ex
    if _STATE["orig"] is not None:
        return
    orig = ex._detect_datetime
    _STATE["orig"] = orig

    def detect(soup):
        d, t = orig(soup)
        try:
            return corrected(soup, d, t)
        except Exception:  # noqa: BLE001  never cost an article its date
            return d, t
    ex._detect_datetime = detect
