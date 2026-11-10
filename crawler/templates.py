"""Client Excel layouts, column for column like the files the team already uses.

- "online"  (news):          DATE, TIME, TITLE, URL, IMAGE, MEDIA, INFLUENCER, PERSON, AUTHOR, SUMMARY, CONTENT, SENTIMENT
- "medsos"  (social posts):  No, Sentiment, Username, User ID, Follower, Text, Date, Time, Likes, Reply, Shares, Url,
                             Emotion, User Image, Platform, Sub Type, Views Count, Gender, Age Category,
                             Content Location, Religion, Ethnic, Phsychography, Parent ID, Post ID, User ID Parent

Columns that need text analysis (sentiment, emotion, people, demographics) are left empty: the app does not use an
AI model. Everything that can be read from the page is filled (some only with "caption & engagement"/"full article").
File names follow the team's pattern: 20261108_Online_Manual_24.00.xlsx (period end date, layout, Manual/Auto,
"24.00" for a whole past day or the time the data runs up to).
"""
from __future__ import annotations

import csv
import os
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill

ONLINE_COLUMNS = ["DATE", "TIME", "TITLE", "URL", "IMAGE", "MEDIA", "INFLUENCER", "PERSON", "AUTHOR", "SUMMARY",
                  "CONTENT", "SENTIMENT"]
ONLINE_WIDTHS = [8.9, 7.3, 21, 13.87, 12.37, 17.37, 21.38, 12.99, 16.37, 19, 12, 9.5]     # the template's
MEDSOS_COLUMNS = ["No", "Sentiment", "Username", "User ID", "Follower", "Text", "Date", "Time", "Likes", "Reply",
                  "Shares", "Url", "Emotion", "User Image", "Platform", "Sub Type", "Views Count", "Gender",
                  "Age Category", "Content Location", "Religion", "Ethnic", "Phsychography", "Parent ID", "Post ID",
                  "User ID Parent"]
MEDSOS_WIDTHS = [6, 11, 18, 16, 11, 60, 11, 9, 13, 13, 13, 40, 12, 30, 10, 13, 12, 9, 13, 16, 9, 13, 14, 20, 13, 18]
PLATFORM_NAME = {"x": "twitter", "instagram": "instagram", "threads": "threads", "facebook": "facebook",
                 "tiktok": "tiktok", "youtube": "youtube"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_IG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


# ---------- small helpers ----------
def _cell(v):
    if isinstance(v, str):
        v = ILLEGAL_CHARACTERS_RE.sub("", v)[:32000]
    return v


def _num(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _local(ts, tz) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(tz)
    except (TypeError, ValueError, OSError):
        return None


def ig_media_id(shortcode: str) -> str:
    """Instagram/Threads shortcode -> numeric media id (the shortcode is that number in a base-64 alphabet)."""
    n = 0
    for ch in (shortcode or "")[:11]:
        i = _IG_ALPHABET.find(ch)
        if i < 0:
            return ""
        n = n * 64 + i
    return str(n) if n else ""


def post_id(platform: str, url: str, user_id: str = "") -> str:
    u = url or ""
    if platform == "x":
        m = re.search(r"/status/(\d+)", u)
        return m.group(1) if m else ""
    if platform == "tiktok":
        m = re.search(r"/(?:video|photo)/(\d+)", u)
        return m.group(1) if m else ""
    if platform == "youtube":
        p = urlparse(u)
        if "/shorts/" in p.path:
            return p.path.rstrip("/").rsplit("/", 1)[-1]
        return (parse_qs(p.query).get("v") or [""])[0] or (p.path.strip("/") if "youtu.be" in p.netloc else "")
    if platform in ("instagram", "threads"):
        m = re.search(r"/(?:p|reel|tv|post)/([A-Za-z0-9_-]+)", u)
        mid = ig_media_id(m.group(1)) if m else ""
        return f"{mid}_{user_id}" if platform == "threads" and mid and user_id else mid
    if platform == "facebook":
        m = re.search(r"(?:story_fbid=|/posts/|/videos/|/reel/|fbid=)(\d{6,})", u)
        return m.group(1) if m else ""
    return ""


def sub_type(platform: str, url: str, known: str = "") -> str:
    if known:
        return known
    if platform == "x":
        return "tweet"
    if platform == "instagram":
        return "video" if re.search(r"/(reel|tv)/", url or "") else "post"
    if platform == "facebook":
        return "video" if re.search(r"/(videos|reel|watch)/", url or "") else "post"
    return "post"


def file_name(layout: str, end_date: str, tz, origin: str = "Manual", folder: str = "") -> str:
    """20261108_Online_Manual_24.00 — "24.00" when the period ends before today, else the current time."""
    now = datetime.now(tz)
    hhmm = "24.00" if end_date < now.strftime("%Y-%m-%d") else now.strftime("%H.%M")
    base = f"{end_date.replace('-', '')}_{layout}_{origin}_{hhmm}"
    name, n = base, 2
    while folder and (os.path.exists(os.path.join(folder, name + ".xlsx"))):
        name = f"{base}_{n}"
        n += 1
    return name


# ---------- rows ----------
def online_row(r: dict, tz) -> list:
    """A news result -> the Online columns, with the team's final upload rules (online_clean.py)."""
    from . import online_clean as oc
    dt = None
    full = (r.get("published_full") or "").strip()            # from the article page, local time "YYYY-MM-DD HH:MM"
    if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", full):
        dt = datetime.strptime(full[:16], "%Y-%m-%d %H:%M")
    elif r.get("ts"):                                         # the Google News feed's exact time
        dt = _local(r["ts"], tz)
    date_s, time_s = "", "00:00:00"                          # unknown time -> 00:00:00 (team rule)
    if dt:
        date_s, time_s = f"{dt.day:02d}-{MONTHS[dt.month - 1]}-{dt.year}", dt.strftime("%H:%M:00" if full else "%H:%M:%S")
    elif r.get("published"):
        try:
            d = datetime.strptime(r["published"][:10], "%Y-%m-%d")
            date_s = f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"
        except ValueError:
            pass
    url = oc.normalize_url(r.get("url"))
    title = (r.get("title") or "").strip()
    text = r.get("text") or ""
    summary = oc.clean_summary(r.get("description") or (re.sub(r"\s+", " ", text)[:400] if text else ""), title)
    return [date_s, time_s, title, url, (r.get("image") or "").strip(), oc.media_from_url(url),
            oc.format_names(r.get("influencer")), oc.format_names(r.get("person")), (r.get("author") or "").strip(),
            summary, oc.content_html(text, title, r.get("author") or ""),
            (r.get("sentiment") or analyze(f"{title}. {summary}")[0]).lower().strip()]


def analyze(text: str) -> tuple[str, str]:
    """(sentiment, emotion) with the team's lexicon analysis (the API scraper's engine); ("", "") if unavailable."""
    if not (text or "").strip():
        return "", ""
    try:
        from .apiscraper.engine import analyze_text
    except Exception:                                    # pandas/requests missing on an old install
        return "", ""
    return analyze_text(text)


def medsos_row(i: int, r: dict, tz) -> list:
    """A social post (keyword search or account collection) -> the Medsos columns."""
    plat = r.get("platform") or ""
    dt = _local(r.get("ts"), tz)
    if not dt and r.get("date"):
        date_s, time_s = r["date"], (r.get("time") or "") + (":00" if len(r.get("time") or "") == 5 else "")
    else:
        date_s, time_s = (dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")) if dt else ("", "")
    uid = str(r.get("user_id") or "")
    text = r.get("caption") or r.get("text") or ""
    sent, emo = (r.get("sentiment") or "", r.get("emotion") or "") if r.get("sentiment") else analyze(text)
    return [i, sent, r.get("author") or r.get("handle") or "", uid, _num(r.get("followers")), text, date_s, time_s,
            _num(r.get("likes")), _num(r.get("comments")), _num(r.get("shares")), r.get("url") or "", emo,
            r.get("user_image") or "", PLATFORM_NAME.get(plat, plat), sub_type(plat, r.get("url"), r.get("sub_type") or ""),
            _num(r.get("views")), "", "", "", "", "", "", "", post_id(plat, r.get("url"), uid), uid]


def rows_for(layout: str, rows: list, tz, drop_empty: bool = False, stats: dict | None = None) -> tuple[list, list]:
    """(columns, values) — used for the Excel/CSV files and for deliveries to another server.
    Online: duplicate URLs dropped (fullest CONTENT kept); drop_empty also drops articles without CONTENT."""
    if layout == "online":
        from . import online_clean as oc
        values, st = oc.finalize([online_row(r, tz) for r in rows], ONLINE_COLUMNS, drop_empty)
        if stats is not None:
            stats.update(st)
        return ONLINE_COLUMNS, values
    return MEDSOS_COLUMNS, [medsos_row(i, r, tz) for i, r in enumerate(rows, 1)]


# ---------- files ----------
def write(layout: str, path_xlsx: str, path_csv: str, rows: list, tz, drop_empty: bool = False) -> None:
    cols, values = rows_for(layout, rows, tz, drop_empty)
    wb = Workbook(write_only=False)
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(cols)
    if layout == "online":
        # exactly like the team's Online_News_Template_Upload_2.xlsx: teal header, grey for INFLUENCER/PERSON/SUMMARY
        from openpyxl.styles import Alignment
        from openpyxl.styles.colors import Color
        font = Font(name="Calibri", size=9, bold=True, color=Color(theme=0))
        teal, grey = PatternFill("solid", fgColor="FF15677B"), PatternFill("solid", fgColor=Color(theme=0, tint=-0.5))
        for c in ws[1]:
            c.font, c.alignment = font, Alignment(horizontal="left", vertical="center")
            c.fill = grey if c.value in ("INFLUENCER", "PERSON", "SUMMARY") else teal
        widths = ONLINE_WIDTHS
        ws.sheet_view.zoomScale = 80
    else:
        fill, font, widths = PatternFill("solid", fgColor="FF4472C4"), Font(name="Calibri", size=11, bold=True,
                                                                                color="FFFFFFFF"), MEDSOS_WIDTHS
        for c in ws[1]:
            c.fill, c.font = fill, font
    for row in values:
        ws.append([_cell(v) for v in row])
        for c in ws[ws.max_row]:
            if isinstance(c.value, str) and c.value[:1] in "=+-@":
                c.data_type = "s"                                 # text that looks like a formula stays text
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    if layout == "medsos":
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=len(cols)).column_letter}{max(1, ws.max_row)}"
    tmp = path_xlsx + ".tmp"
    wb.save(tmp)
    os.replace(tmp, path_xlsx)
    tmp = path_csv + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for row in values:
            w.writerow(["'" + v if isinstance(v, str) and v[:1] in "=+-@" else ("" if v is None else v) for v in row])
    os.replace(tmp, path_csv)

