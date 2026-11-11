"""Missed-clip gap audit.

Paste the links you EXPECTED to see (e.g. a competitor's clip list) and
get a verdict for every one:

- CAUGHT (+ its status: success / needs review / failed / queued /
  filtered) — the clip is in the archive; maybe just not in the file
  that was being looked at.
- MISSED, outlet not in dictionary — add the outlet, it joins the
  whitelist and future harvesting.
- MISSED, URL rejected by the article filter — the URL shape looks like
  a tag/listing page; each one found here is a filter bug to fix.
- MISSED, outlet marked unreachable — the health check flagged the site.
- MISSED, never discovered — the true engine gap: sources did not
  surface the link. The audit notes whether the outlet's sitemap was
  ever checked and whether an RSS feed is known, which tells WHERE the
  gap is.

The result is written to audit_report_<timestamp>.xlsx (one row per
link + a summary sheet) — a shareable diagnosis of any recall gap.
"""

import os
import re
import time
from collections import Counter

from .filters import is_article_link
from .media import in_dictionary, url_domain


def _norm(url):
    u = str(url or "").strip().strip('"').strip(",;")
    u = re.sub(r"^https?://", "", u)
    u = u[4:] if u.startswith("www.") else u
    return u.rstrip("/").lower()


def gap_audit(db, urls, output_folder="output", log=print):
    """Audit expected links against the archive. Returns (path, summary)."""
    urls = [u.strip() for u in urls if u.strip().lower().startswith(
        ("http://", "https://", "www."))]
    urls = list(dict.fromkeys(urls))
    if not urls:
        log("⚠️ Gap check: no valid links pasted.")
        return None, {}
    log(f"🔍 Gap check: auditing {len(urls)} expected links against the "
        f"archive...")

    # normalized index of everything the engine ever saw
    archive = {}
    for r in db._conn.execute("SELECT url, status FROM links"):
        archive[_norm(r["url"])] = (r["url"], r["status"])
    queued = {_norm(r["url"]) for r in
              db._conn.execute("SELECT url FROM decode_queue")}
    health = {r["domain"]: r["fails"] for r in
              db._conn.execute("SELECT domain, fails FROM domain_health")}
    sm_checked = {r["domain"] for r in
                  db._conn.execute("SELECT domain FROM sitemap_stats "
                                   "WHERE checks > 0")}
    feeds = db.known_feeds()

    status_label = {"success": "CAUGHT — in the report (success)",
                    "needs_review": "CAUGHT — in the Needs Review sheet",
                    "failed": "CAUGHT — found but download failed",
                    "new": "CAUGHT — found, still queued for download",
                    "filtered": "CAUGHT — found but filtered (date/not article)"}
    rows, summary = [], Counter()
    for u in urls:
        full = u if u.startswith("http") else "https://" + u
        n = _norm(full)
        dom = url_domain(full)
        if n in archive:
            _, st = archive[n]
            verdict = status_label.get(st, f"CAUGHT — status {st}")
            detail = ""
        elif n in queued:
            verdict = "CAUGHT — waiting in the decode queue"
            detail = "will resolve on the next run"
        elif not in_dictionary(full):
            verdict = "MISSED — outlet not in your media dictionary"
            detail = f"add '{dom}' to the dictionary / extra media"
        elif not is_article_link(full):
            verdict = "MISSED — URL rejected by the article filter"
            detail = "looks like a tag/listing URL; report this row to fix the filter"
        elif health.get(dom, 0) >= 2:
            verdict = "MISSED — outlet marked unreachable by the health check"
            detail = "re-checked automatically after 14 days"
        else:
            verdict = "MISSED — never discovered by any source"
            hints = []
            hints.append("sitemap checked before" if dom in sm_checked
                         else "sitemap NEVER checked yet (rotation)")
            hints.append("RSS feed known" if dom in feeds
                         else "no RSS feed known yet")
            detail = "; ".join(hints)
        summary[verdict.split(" — ")[0] + " — " + verdict.split(" — ")[1]] += 1
        rows.append({"URL": full, "Outlet": dom, "Verdict": verdict,
                     "Detail": detail})

    caught = sum(v for k, v in summary.items() if k.startswith("CAUGHT"))
    log(f"   ➜ {caught}/{len(urls)} are actually in the engine already; "
        f"{len(urls) - caught} genuinely missed.")
    for k, v in summary.most_common():
        log(f"      {v:>4} × {k}")
    if caught < len(urls):
        log("   💡 Paste the MISSED ones into Bulk import to deliver them "
            "in today's report — and send this audit file back for "
            "permanent fixes.")

    import pandas as pd
    os.makedirs(output_folder, exist_ok=True)
    path = os.path.join(output_folder,
                        f"audit_report_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        pd.DataFrame(rows).to_excel(w, sheet_name="Links", index=False)
        pd.DataFrame([{"Verdict": k, "Count": v}
                      for k, v in summary.most_common()]).to_excel(
            w, sheet_name="Summary", index=False)
    log(f"📄 Gap audit report: {path}")
    return path, dict(summary)
