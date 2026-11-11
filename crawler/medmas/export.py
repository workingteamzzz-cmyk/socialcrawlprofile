"""Export results from the database:
1. Internal format — Excel (Data + Needs Review + Recap sheets) and CSV.
2. Upload Online format — matches the media-monitoring dashboard template:
   DATE, TIME, TITLE, URL, IMAGE NEWS, MEDIA, SUMMARY, CONTENT, SENTIMENT,
   INFLUENCER, PERSON, ORGANIZATION, LOCATION, JOURNALIST, ISSUE.
"""

import html as _html
import os
import re
import time
from datetime import datetime

import pandas as pd

from .extract import clean_content, clean_title, looks_like_index_page
from .filters import keyword_excluded, keyword_match, url_slug
from .media import media_outlet

COLUMNS = ["No", "URL", "Media", "Title", "Author", "Date", "Time",
           "Keyword", "Source", "Image", "Influencer", "Persons",
           "Organizations", "Sentiment",
           "Summary", "Content", "Duplicate", "Status"]


def _normalize_title(title):
    t = re.sub(r"[^a-z0-9 ]", " ", str(title).lower())
    return " ".join(t.split())


def _mark_duplicates(rows):
    """Same title (first 10 words) -> cross-media duplicate."""
    seen = {}
    for r in rows:
        key = " ".join(_normalize_title(r["Title"]).split()[:10])
        if not key:
            r["Duplicate"] = ""
        elif key in seen:
            r["Duplicate"] = f"Duplicate of No {seen[key]}"
        else:
            seen[key] = r["No"]
            r["Duplicate"] = ""


def _recap(df):
    recap = {}
    df_ok = df[df["Status"] == "success"]
    recap["📌 OVERVIEW"] = pd.DataFrame({
        "Item": ["Total links", "Extracted successfully",
                 "Needs manual review (keyword unverified)",
                 "Failed/empty", "Duplicates detected", "Unique outlets"],
        "Value": [len(df), len(df_ok),
                  int((df["Status"] == "needs_review").sum()),
                  int((df["Status"] == "failed").sum()),
                  int((df["Duplicate"].astype(str) != "").sum()),
                  df["Media"].nunique()],
    })
    per_media = df["Media"].value_counts().reset_index()
    per_media.columns = ["Media", "Count"]
    recap["📰 ARTICLES PER OUTLET"] = per_media
    per_kw = df["Keyword"].replace("", "(imported)").value_counts().reset_index()
    per_kw.columns = ["Keyword", "Count"]
    recap["🔎 ARTICLES PER KEYWORD"] = per_kw
    per_src = df["Source"].value_counts().reset_index()
    per_src.columns = ["Link Source", "Count"]
    recap["🌐 LINKS PER SOURCE"] = per_src
    dates = df[df["Date"].astype(str) != ""]["Date"].astype(str).str[:10]
    if len(dates):
        per_date = dates.value_counts().sort_index().reset_index()
        per_date.columns = ["Date", "Count"]
        recap["📅 ARTICLES PER DATE"] = per_date
    sent = df[df["Sentiment"].astype(str) != ""]["Sentiment"]
    if len(sent):
        per_sent = sent.value_counts().reset_index()
        per_sent.columns = ["Sentiment", "Count"]
        recap["🧠 SENTIMENT"] = per_sent
    inf = (df[df["Influencer"].astype(str) != ""]["Influencer"]
           .str.split(", ").explode())
    if len(inf):
        top_inf = inf.value_counts().head(15).reset_index()
        top_inf.columns = ["Influencer", "Articles"]
        recap["🗣️ TOP INFLUENCERS"] = top_inf
    au = df[df["Author"].astype(str).replace("None", "") != ""]["Author"]
    if len(au):
        top_au = au.value_counts().head(15).reset_index()
        top_au.columns = ["Author", "Articles"]
        recap["✍️ TOP AUTHORS"] = top_au
    return recap


def export(db, output_folder, keyword=None, log=print, batch=None,
           start=None, end=None):
    """Write crawl_results_<timestamp>.xlsx + .csv. Return the Excel path.
    batch: limit the file to one run instead of the whole archive.
    start/end (YYYY-MM-DD): limit to articles published in that timeframe —
    the on-demand archive download ('give me everything from Aug 10'),
    regardless of which run collected the articles."""
    results = db.all_results(keyword, batch=batch)
    if start or end:
        lo, hi = start or "0000-00-00", end or "9999-12-31"
        results = [h for h in results
                   if (h["published_date"] or "")[:10]
                   and lo <= h["published_date"][:10] <= hi]
        log(f"📅 Archive timeframe {lo} → {hi}: {len(results)} articles.")
    if not results:
        log("⚠️ No data to export yet.")
        return None
    rows = []
    for i, h in enumerate(results, 1):
        rows.append({
            "No": i, "URL": h["url"], "Media": media_outlet(h["url"]),
            "Title": clean_title(h["title"], h["url"]),
            "Author": h["author"] or "",
            "Date": (h["published_date"] or "")[:10],
            "Time": h["published_time"] or "",
            "Keyword": h["keyword"] or "", "Source": h["source"] or "",
            "Image": h["image"] or "", "Influencer": h["influencer"] or "",
            "Persons": h["persons"] or "", "Sentiment": h["sentiment"] or "",
            "Organizations": (h["organizations"]
                              if "organizations" in h.keys() else "") or "",
            "Summary": clean_content(h["summary"] or "").replace("\n", " "),
            "Content": clean_content(h["content"] or "")[:32000],
            "Duplicate": "", "Status": h["status"] or "",
        })
    _mark_duplicates(rows)
    df = pd.DataFrame(rows, columns=COLUMNS)

    os.makedirs(output_folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path_xlsx = os.path.join(output_folder, f"crawl_results_{stamp}.xlsx")
    path_csv = os.path.join(output_folder, f"crawl_results_{stamp}.csv")

    with pd.ExcelWriter(path_xlsx, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Data", index=False)
        # Human review queue — the key to minimizing missed clips:
        # doubtful articles & failed links are never lost, review them here.
        df_check = df[df["Status"].isin(["needs_review", "failed"])]
        if len(df_check):
            df_check.drop(columns=["Content"]).to_excel(
                writer, sheet_name="Needs Review", index=False)
        pos = 0
        for title, table in _recap(df).items():
            pd.DataFrame([[title]]).to_excel(writer, sheet_name="Recap",
                                             startrow=pos, index=False, header=False)
            table.to_excel(writer, sheet_name="Recap", startrow=pos + 1, index=False)
            pos += len(table) + 4
    df.drop(columns=["Content"]).to_csv(path_csv, index=False)
    log(f"📄 Excel : {path_xlsx}")
    log(f"📄 CSV   : {path_csv}")
    return path_xlsx


# ============================================================
#          UPLOAD ONLINE FORMAT (dashboard template)
# ============================================================
UPLOAD_COLUMNS = ["DATE", "TIME", "TITLE", "URL", "IMAGE NEWS", "MEDIA",
                  "SUMMARY", "CONTENT", "SENTIMENT", "INFLUENCER", "PERSON",
                  "ORGANIZATION", "LOCATION", "JOURNALIST", "ISSUE"]


def _entities(text):
    """Escape HTML + turn non-ASCII characters into entities (&#8211; etc.)."""
    return (_html.escape(str(text or ""), quote=False)
            .encode("ascii", "xmlcharrefreplace").decode("ascii"))


def _split_wall(p, target=450):
    """A wall of text with no line breaks -> readable paragraph chunks,
    split at sentence boundaries (~450 chars each). Some sites lose their
    paragraph breaks during extraction; one giant <p> looks broken on the
    dashboard, so we rebuild paragraphs from sentences."""
    sentences = re.split(r"(?<=[.!?…])\s+(?=[A-Z0-9\"'“‘])", p)
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > target:
            chunks.append(cur.strip())
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        chunks.append(cur.strip())
    return chunks


def _content_html(text):
    """Plain extracted text -> <p>...</p> paragraphs with entities.

    clean_content() runs again here so articles stored in the database by
    OLDER engine versions (before the junk rules existed) also export clean
    — no re-crawl needed. Cleaning already-clean text changes nothing.
    The </p><p> separators are always preserved: the dashboard needs them
    for paragraph spacing. Paragraphs longer than ~700 chars (extraction
    lost the line breaks) are re-split at sentence boundaries."""
    text = clean_content(text)
    if not text:
        return ""
    paragraphs = []
    for p in re.split(r"\n+", str(text)):
        p = p.strip()
        if not p:
            continue
        if len(p) > 700:
            paragraphs.extend(_split_wall(p))
        else:
            paragraphs.append(p)
    return "".join(f"<p>{_entities(p)}</p>" for p in paragraphs)


def _upload_date(d):
    """2026-06-16 -> 16-Jun-2026 (template format)."""
    d = str(d or "")[:10]
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d-%b-%Y")
    except ValueError:
        return d


def _people_list(value):
    """'Iwan, Budi Santoso' -> 'iwan,budi santoso' (template style)."""
    return ",".join(n.strip().lower() for n in str(value or "").split(",")
                    if n.strip())


def export_upload(db, output_folder, keyword=None, log=print,
                  include_needs_review=False, start=None, end=None, batch=None):
    """Write Upload_Online_<timestamp>.xlsx in the dashboard template format.
    Only 'success' articles (optionally + 'needs_review').
    When start/end are given the file STRICTLY contains that timeframe:
    rows must carry a valid date inside [start, end]. Return the path."""
    ok = {"success"} | ({"needs_review"} if include_needs_review else set())
    # SECOND CHANCE at export time — statuses were frozen when the article
    # was extracted, but the rules improve (e.g. the curly-quote keyword
    # fix) and archive requests use different date windows:
    # - needs_review whose keyword NOW verifies -> promoted (always)
    # - filtered (date was outside that run's window) -> promoted in
    #   ARCHIVE mode; the timeframe filter downstream still enforces the
    #   requested window, so nothing out-of-range leaks in
    results, promoted_nr, promoted_flt = [], 0, 0
    for h in db.all_results(keyword, batch=batch):
        st = h["status"]
        if st in ok:
            results.append(h)
            continue
        if not h["content"] or not h["keyword"]:
            continue
        if st == "needs_review" or (st == "filtered" and start and end):
            slug = url_slug(h["url"])
            if keyword_excluded(h["keyword"], h["title"], h["content"], slug):
                continue
            if keyword_match(h["keyword"], h["title"], h["content"], slug):
                results.append(h)
                if st == "needs_review":
                    promoted_nr += 1
                else:
                    promoted_flt += 1
    if promoted_nr:
        log(f"🔁 {promoted_nr} needs-review articles re-verified at export "
            f"(keyword matches under current rules) — promoted into the report.")
    if promoted_flt:
        log(f"📦 {promoted_flt} previously date-filtered articles fall "
            f"inside the requested window — recovered into the report.")
    # Legacy rows: tag/search listings collected before index detection
    # existed must not reach the dashboard as articles.
    listings = [h for h in results if looks_like_index_page(h["title"], h["url"])]
    if listings:
        drop = {h["url"] for h in listings}
        results = [h for h in results if h["url"] not in drop]
        log(f"🚫 {len(listings)} tag/search listing pages excluded "
            f"(not news articles).")
    # Undated articles are NOT thrown away (anti missed clip): the day we
    # DISCOVERED the link stands in for the publish date — a clip found
    # inside the monitoring window belongs to that window.
    dated = []
    for h in results:
        h = dict(h)
        d = str(h["published_date"] or "")[:10]
        if not re.match(r"\d{4}-\d{2}-\d{2}", d):
            d = str(h.get("discovered_at") or "")[:10]
            if re.match(r"\d{4}-\d{2}-\d{2}", d):
                h["published_date"] = d
        dated.append(h)
    results = dated
    if start and end:
        before = len(results)
        results = [h for h in results
                   if re.match(r"\d{4}-\d{2}-\d{2}",
                               str(h["published_date"] or "")[:10])
                   and start <= str(h["published_date"])[:10] <= end]
        dropped = before - len(results)
        if dropped:
            log(f"🎯 Upload Online timeframe {start} to {end}: "
                f"{dropped} articles outside the range excluded "
                f"(undated ones already inherit their discovery date).")
    if not results:
        log("⚠️ No successful articles in the timeframe for the Upload Online format.")
        return None
    from .organizations import extract_orgs
    rows = []
    for h in results:
        orgs = h["organizations"] if "organizations" in h.keys() else ""
        if not orgs and h["content"]:
            # rows extracted before the organization detector existed
            orgs = ", ".join(extract_orgs(h["content"]))
        rows.append({
            "DATE": _upload_date(h["published_date"]),
            "TIME": ((h["published_time"] + ":00") if h["published_time"]
                     else "00:00:00"),   # template style for unknown times
            "TITLE": _entities(clean_title(h["title"], h["url"])),
            "URL": h["url"],
            "IMAGE NEWS": h["image"] or "",
            "MEDIA": media_outlet(h["url"]),  # straight from the news URL
            "SUMMARY": clean_content(h["summary"] or "").replace("\n", " "),
            "CONTENT": _content_html(h["content"]),
            "SENTIMENT": (h["sentiment"] or "").lower(),
            "INFLUENCER": _people_list(h["influencer"]),
            "PERSON": _people_list(h["persons"]),
            "ORGANIZATION": _people_list(orgs),
            "LOCATION": "",
            "JOURNALIST": h["author"] or "",
            "ISSUE": h["keyword"] or "",
        })
    df = pd.DataFrame(rows, columns=UPLOAD_COLUMNS)
    os.makedirs(output_folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_folder, f"Upload_Online_{stamp}.xlsx")
    df.to_excel(path, index=False)
    log(f"📤 Upload Online: {path} ({len(df)} articles ready for the dashboard)")
    return path
