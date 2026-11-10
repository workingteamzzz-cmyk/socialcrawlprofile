"""The team's final rules for the Online upload file (from their "Uploader Online v2" notebook), applied by the app
itself so the Excel is ready to upload:

- URL: spaces and a trailing "/" removed; duplicates by that URL dropped, keeping the row with the fullest CONTENT
- DATE 27-Aug-2026, TIME 04:46:00 (unknown time -> 00:00:00)
- MEDIA: the URL's domain without www./amp./m./mobile.
- SUMMARY: broken characters fixed, portal UI junk, a repeated title and date stamps at the start removed
- CONTENT: one <p> per paragraph; portal boilerplate removed (anti-crawling notices, Pewarta/Editor credits, desk codes
  like (dai/dai), Google News promos, reaction widgets, comment boxes, donation blocks, the author's name at the end,
  repeated paragraphs); paragraphs broken mid-sentence joined again
- articles whose CONTENT is empty after cleaning are dropped (when full articles were read)
- INFLUENCER / PERSON lower case, comma separated; SENTIMENT lower case
- cells made safe for Excel (control characters, 32,767 characters, CONTENT cut at a paragraph end)
"""
from __future__ import annotations

import re
from datetime import datetime

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

EXCEL_CELL_MAX = 32767
_MOJIBAKE = [("â€“", "–"), ("â€”", "—"), ("â€œ", "“"),
             ("â€\u009d", "”"), ("â€™", "'"), ("â€˜", "'")]


def format_date(tgl) -> str:
    """2026-08-27 -> 27-Aug-2026"""
    tgl = str(tgl or "").strip()
    if not tgl:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(tgl, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    return tgl


def format_time(jam) -> str:
    """04:46 -> 04:46:00; unknown -> 00:00:00"""
    jam = str(jam or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", jam):
        return jam + ":00"
    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", jam):
        return jam
    return "00:00:00"


def format_names(teks) -> str:
    """'Prabowo Subianto, Bahlil' -> 'prabowo subianto,bahlil' (lower case, no space after comma, no duplicates)"""
    out, seen = [], set()
    for nama in str(teks or "").split(","):
        n = nama.strip().lower()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return ",".join(out)


def normalize_url(url) -> str:
    """https://situs.com/judul-berita/ -> https://situs.com/judul-berita"""
    return str(url or "").strip().rstrip("/")


def media_from_url(url) -> str:
    """The URL's domain without www./amp./m./mobile. (like =SUBSTITUTE(INDEX(SPLIT(url,"/"),2),"www.",""))"""
    url = str(url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parts = [b for b in url.split("/") if b]
    if len(parts) < 2:
        return url
    domain = parts[1].lower().split(":")[0]
    for prefix in ("www.", "amp.", "m.", "mobile."):
        if domain.startswith(prefix) and domain.count(".") >= 2:
            domain = domain[len(prefix):]
    return domain


BULAN_ID = (r"(Jan(?:uari)?|Feb(?:ruari)?|Mar(?:et)?|Apr(?:il)?|Mei|Jun(?:i)?|Jul(?:i)?|Agu(?:stus)?|Sep(?:tember)?|"
            r"Okt(?:ober)?|Nov(?:ember)?|Des(?:ember)?)")
HARI_ID = r"(Senin|Selasa|Rabu|Kamis|Jumat|Jum'at|Sabtu|Minggu)"
_STAMP1 = rf"^\d{{1,2}}\s+{BULAN_ID}\s+\d{{4}}[,\s]+\d{{1,2}}[:.]\d{{2}}\s*(WIB|WITA|WIT)?\s*"
_STAMP2 = rf"^{HARI_ID},?\s+\d{{1,2}}\s+{BULAN_ID}\s+\d{{4}}[,\s]*(\d{{1,2}}[:.]\d{{2}})?\s*(WIB|WITA|WIT)?\s*"


def _fix_chars(s: str) -> str:
    for a, b in _MOJIBAKE:
        s = s.replace(a, b)
    return s


def clean_summary(summary, title) -> str:
    """Broken characters fixed; portal UI junk, a repeated title, date stamps and a source credit at the start removed."""
    s, t = str(summary or "").strip(), str(title or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s+", " ", _fix_chars(s)).strip()
    s = re.sub(r"-?\s*account_circle.*?print Cetak\s*", " ", s)
    s = re.sub(r"info Atur ukuran teks artikel ini untuk mendapatkan pengalaman membaca terbaik\.?", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    changed = True
    while changed:
        changed = False
        if t and s.lower().startswith(t.lower()):
            s = s[len(t):].lstrip(" -–—:.|")
            changed = True
        s2 = re.sub(_STAMP1, "", s, flags=re.I)
        s2 = re.sub(_STAMP2, "", s2, flags=re.I)
        s2 = re.sub(r"^\([^)]{1,50}\)\.?\s*", "", s2)
        if s2 != s:
            s, changed = s2.strip(), True
    return s.strip()


_CREDIT = (r"(?:[-–—|]\s*)?(Penulis|Editor|Penyunting|Pewarta|Reporter|Uploader|Kontributor|Redaktur|Sumber)"
           r"\s*:\s*[A-Za-z0-9 .,'’\-]*$")


def _clean_fragment(p: str) -> str:
    """Portal boilerplate removed from one paragraph: copyright/anti-crawling notices, Google News promos, ads,
    editorial credits, broken characters."""
    p = _fix_chars(p)
    p = re.sub(r"Dilarang keras mengambil konten[^.]*\.", " ", p)
    p = re.sub(r"Scroll ke bawah untuk melanjutkan membaca\s*(Iklan)?\s*", " ", p)
    p = re.sub(r"info Atur ukuran teks artikel ini untuk mendapatkan pengalaman membaca terbaik\.?", " ", p)
    p = re.sub(r"COPYRIGHT\s*©[^.]*\.?", " ", p, flags=re.I)
    p = re.sub(r"[^.!?]*\bdi Google News\b[^.]*\.?", " ", p)
    p = re.sub(r"\*+\)?\s*Ikuti berita terbaru[^.]*(jangan lupa Follow)?", " ", p)
    p = re.sub(r"ADVERTISEMENT|SCROLL TO CONTINUE WITH CONTENT", " ", p, flags=re.I)
    p = re.sub(r"\s+", " ", p).strip()
    while re.search(_CREDIT, p):
        p = re.sub(_CREDIT, "", p).strip()
    return p


_JUNK_PARAGRAPHS = [                       # paragraphs that are ONLY portal junk (matched whole)
    r"What'?s Your Reaction\??", r"\d+\s?(Like|Dislike|Funny|Angry|Sad|Wow|Love|Haha)",
    r"Baca lebih la\w*\s*»?",
    r"Komentar\s*\(\d+\)", r"Belum ada komentar.*", r"Tulis Komentar", r"Berikan Komentar",
    r"Dark/Light Mode", r"Headline", r"Highlight", r"B-FILES", r"BERITA LAINNYA",
    r"Jangan Sampai (Terlewat|ketinggalan.*)", r"Jadilah yang pertama tahu.*",
    r"Baca (berita|informasi) .* di .*Liputan6",
    r".*Google News.*",
    r"⭐?\s*Jadikan .* Sumber Pilihan", r"Dukung Jurnalisme Independen.*", r"Agar berita terbaru.*Google.*",
    r".*Telah diverifikasi oleh Dewan Pers.*",
    r"المصدر:.*",
    r"\|?\s*\d{1,2}[:.]\d{2}\s*(WIB|WITA|WIT)",
    r"\([a-z]{2,4}/[a-z]{2,4}\)",
    r"Jurnalis/?",
    r"Redaksi Popnews", r"Tim editorial yang menghadirkan.*", r"Editor .*Popnews",
]
_RE_JUNK = re.compile(r"^(?:" + "|".join(_JUNK_PARAGRAPHS) + r")$", re.I)
_JUNK_BLOCKS = {"apresiasi pembaca": re.compile(r"apresiasi|kontribusi|Syarat dan Ketentuan|Murianews", re.I)}


def content_html(teks, title="", author="") -> str:
    """One <p> per paragraph, with portal boilerplate removed, a repeated title / date stamp at the start removed,
    the author's name at the end removed, repeated paragraphs removed and broken paragraphs joined."""
    teks, t = str(teks or "").strip(), str(title or "").strip()
    if not teks:
        return ""
    teks = re.sub(r"-?\s*account_circle[\s\S]{0,400}?(pengalaman membaca terbaik\.?|jam yang lalu"
                  r"(\s*-\s*comment\s*\d+\s*komentar)?(\s*-\s*print\s*Cetak)?)", "\n", teks)
    frag = []
    for p in re.split(r"\n+|\s{2,}", teks):
        p = re.sub(r"\s+", " ", p).strip()
        p = re.sub(r"^[-–—•_*]+\s*", "", p).strip()
        p = _clean_fragment(p)
        if p and p not in ("_", "-", "•", "***", "Iklan") and not _RE_JUNK.match(p):
            frag.append(p)
    kept, block = [], None
    for p in frag:                                       # multi-paragraph junk blocks (e.g. Murianews' "Apresiasi Pembaca")
        if p.lower() in _JUNK_BLOCKS:
            block = _JUNK_BLOCKS[p.lower()]
            continue
        if block is not None and block.search(p):
            continue
        block = None
        kept.append(p)
    frag = [p for k, p in enumerate(kept) if k == 0 or p != kept[k - 1]]
    authors = {n.strip().lower() for n in re.split(r"[;,]", str(author or "")) if n.strip()}
    while frag and frag[-1].strip().lower() in authors:
        frag.pop()
    while frag:
        f0, changed = frag[0], False
        if t and f0.lower().startswith(t.lower()):
            f0, changed = f0[len(t):].lstrip(" -–—:.|"), True
        f2 = re.sub(_STAMP1, "", f0, flags=re.I)
        f2 = re.sub(_STAMP2, "", f2, flags=re.I)
        if f2 != f0:
            f0, changed = f2.strip(), True
        if not f0.strip():
            frag.pop(0)
            continue
        frag[0] = f0.strip()
        if not changed:
            break
    paras = []
    for p in frag:
        if paras and not re.search(r'[.!?:;"”’)\]]$', paras[-1]) and re.match(r"^[a-z]", p):
            paras[-1] = paras[-1] + " " + p                 # a paragraph broken in the middle of a sentence
            continue
        paras.append(p)
    return "".join(f"<p>{p}</p>" for p in paras)


def safe_cell(v, column: str = ""):
    """Control characters removed; longer than an Excel cell -> cut (CONTENT at the end of a paragraph)."""
    if not isinstance(v, str):
        return v
    v = re.sub(r"_x[0-9A-Fa-f]{4}_", "", v)
    v = ILLEGAL_CHARACTERS_RE.sub("", v)
    if len(v) > EXCEL_CELL_MAX:
        cut = v[:EXCEL_CELL_MAX]
        if column == "CONTENT" and cut.rfind("</p>") != -1:
            cut = cut[:cut.rfind("</p>") + 4]
        v = cut
    return v


def finalize(rows: list[list], columns: list[str], drop_empty_content: bool) -> tuple[list[list], dict]:
    """Drop empty-CONTENT articles (optional) and duplicate URLs (keeping the fullest CONTENT), keep the order."""
    ci, ui = columns.index("CONTENT"), columns.index("URL")
    dropped_empty = [r[ui] for r in rows if drop_empty_content and not (r[ci] or "").strip()]
    if drop_empty_content:
        rows = [r for r in rows if (r[ci] or "").strip()]
    best: dict[str, int] = {}
    for i, r in enumerate(rows):
        j = best.get(r[ui])
        if j is None or len(r[ci] or "") > len(rows[j][ci] or ""):
            best[r[ui]] = i
    keep = set(best.values())
    dupes = [r[ui] for i, r in enumerate(rows) if i not in keep]
    rows = [r for i, r in enumerate(rows) if i in keep]
    return [[safe_cell(v, columns[k]) for k, v in enumerate(r)] for r in rows], {"empty": dropped_empty, "duplicates": dupes}
