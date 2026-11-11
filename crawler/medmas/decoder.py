"""Decode news.google.com redirect links into the real article URLs.

3 stages: googlenewsdecoder fast -> slow retry (avoids rate limits) ->
scrape-the-redirect-page fallback. Links that STILL fail get a final
rescue: we already know each link's TITLE and OUTLET from the feed, so we
search Bing for the title and take the matching outlet's URL — no Google
decoding involved at all. Whatever remains goes to the retry queue.
"""

import difflib
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def _decode_one(url, interval):
    try:
        from googlenewsdecoder import gnewsdecoder
        d = gnewsdecoder(url, interval=interval)
        if d.get("status") and d.get("decoded_url", "").startswith("http"):
            return d["decoded_url"]
    except Exception:
        pass
    return None


def _via_redirect(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        m = re.search(
            r'href="(https?://(?!news\.google|accounts\.google|policies\.google)[^"]+)"',
            r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _run_pass(items, worker, threads, label, log, started, budget_minutes,
              should_stop):
    """Decode a batch in chunks so progress, ETA, the time budget and the
    Stop button all stay responsive."""
    out, done = {}, 0
    chunk = max(threads * 4, 40)
    t0 = time.time()
    for i in range(0, len(items), chunk):
        if should_stop() or (budget_minutes
                             and time.time() - started > budget_minutes * 60):
            break
        part = items[i:i + chunk]
        with ThreadPoolExecutor(max_workers=threads) as ex:
            for t, res in zip(part, ex.map(worker, part)):
                out[t["url"]] = res
        done += len(part)
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        left = (len(items) - done) / rate if rate > 0 else 0
        ok = sum(1 for v in out.values() if v)
        log(f"      {label}: {done}/{len(items)} · ✅ {ok} · "
            f"⚡ {rate:.1f}/s · ~{left / 60:.0f} min left")
    return out


def decode_all(findings, log=print, threads=5, budget_minutes=0,
               should_stop=lambda: False):
    """findings: list of dicts with a 'url' key. Returns (decoded, leftovers):
    decoded has real URLs; leftovers are the Google News entries that could
    not be decoded this run (the caller queues them for a later retry)."""
    targets = [t for t in findings if "news.google.com" in t["url"]]
    others = [t for t in findings if "news.google.com" not in t["url"]]
    if not targets:
        return findings, []
    started = time.time()

    def over_budget():
        return bool(budget_minutes) and (time.time() - started) > budget_minutes * 60
    # NOTE: this is link decoding, unrelated to the "Retry failed" option
    # (which re-queues previously failed links from earlier runs).
    log(f"🔓 Decoding {len(targets)} Google News links into real article "
        f"URLs — pass 1/3, {threads} parallel...")

    mapping = dict.fromkeys((t["url"] for t in targets), None)
    mapping.update(_run_pass(targets, lambda t: _decode_one(t["url"], 0.2),
                             threads, "pass 1/3", log, started, budget_minutes,
                             should_stop))

    failed1 = [t for t in targets if mapping[t["url"]] is None]
    if should_stop():
        log("   ⛔ Stop requested — keeping the links decoded so far.")
        failed1 = []
    if failed1 and over_budget():
        log(f"   ⏱️ Decode budget of {budget_minutes} min reached after pass 1 — "
            f"stopping here with {len(targets) - len(failed1)} decoded. "
            f"The remaining articles are normally also reachable via "
            f"sitemap/RSS/Bing.")
        failed1 = []
    if failed1:
        rate = len(failed1) / max(1, len(targets))
        log(f"   🔓 Decode pass 2/3: {len(failed1)} of {len(targets)} links "
            f"need a slower second attempt "
            f"(Google throttles bulk decoding; this is not the 'Retry failed' option).")
        if rate > 0.4:
            log("      ℹ️ High throttling right now — this pass takes a few "
                "minutes. Nothing is lost; undecodable links are reported below.")
        mapping.update(_run_pass(failed1, lambda t: _decode_one(t["url"], 1.0),
                                 2, "pass 2/3", log, started, budget_minutes,
                                 should_stop))

    failed2 = [t for t in targets if mapping[t["url"]] is None]
    # Pass 3 fetches one redirect page per link (15 s timeout each): when
    # Google is throttling hard it rarely pays off, so skip it on big piles.
    if failed2 and should_stop():
        failed2 = []
    if failed2 and (over_budget() or len(failed2) > 800):
        why = ("budget reached" if over_budget()
               else f"{len(failed2)} leftovers would take ~"
                    f"{len(failed2) * 5 / 60 / max(1, threads):.0f} min for few hits")
        log(f"   ⏭️ Skipping decode pass 3 ({why}). Those links are kept "
            f"and retried automatically on the next run.")
        failed2 = []
    if failed2:
        log(f"   🔓 Decode pass 3/3: resolving {len(failed2)} leftovers "
            f"by following the redirect page...")
        with ThreadPoolExecutor(max_workers=threads) as ex:
            for t, res in zip(failed2, ex.map(lambda t: _via_redirect(t["url"]), failed2)):
                mapping[t["url"]] = res

    final, leftovers = list(others), []
    for t in targets:
        real = mapping.get(t["url"])
        if real:
            t = dict(t)
            t["decoded_from"] = t["url"]   # so the caller can clear queues
            t["url"] = real
            final.append(t)
        else:
            leftovers.append(t)
    ok = len(targets) - len(leftovers)
    log(f"   ✅ Decoded {ok}/{len(targets)} Google News links "
        f"({ok / max(1, len(targets)) * 100:.0f}%).")
    return final, leftovers


# ==================================================================
#  TITLE RESCUE — recover undecodable Google links via Bing search
# ==================================================================
def _norm_title(s):
    return re.sub(r"[^a-z0-9 ]", "", str(s or "").lower()).strip()


def _same_outlet(url, domain):
    if not domain:
        return False
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    host = host[4:] if host.startswith("www.") else host
    d = domain.lower()
    d = d[4:] if d.startswith("www.") else d
    return host == d or host.endswith("." + d) or d.endswith("." + host)


def resolve_by_search(items, log=print, threads=4, limit=200,
                      should_stop=lambda: False):
    """Find real URLs for undecodable Google links by searching Bing for
    their TITLE (known from the feed). Returns (resolved, still_failed).
    Links with a known outlet are tried first — an exact outlet match is
    proof; otherwise a near-identical title is accepted."""
    from .discovery import bing_news

    todo = [t for t in items if len(str(t.get("title", ""))) >= 20]
    skipped = [t for t in items if t not in todo]
    todo.sort(key=lambda t: not t.get("domain"))     # domain-known first
    todo, overflow = todo[:limit], todo[limit:]
    if not todo:
        return [], items
    log(f"   🛟 Title rescue: searching Bing for {len(todo)} undecodable "
        f"links by their titles...")

    def worker(t):
        title = str(t["title"])
        dom = str(t.get("domain") or "")
        query = f"{title} site:{dom}" if dom else f'"{title}"'
        try:
            found = bing_news.harvest(query, log=lambda *a: None)
        except Exception:
            found = []
        if dom:
            for f in found:
                if _same_outlet(f["url"], dom):
                    return f["url"]
        best, best_ratio = None, 0.0
        for f in found:
            ratio = difflib.SequenceMatcher(
                None, _norm_title(title), _norm_title(f["title"])).ratio()
            if ratio > best_ratio:
                best, best_ratio = f["url"], ratio
        return best if best_ratio >= 0.8 else None

    resolved, failed = [], list(skipped) + list(overflow)
    chunk = max(threads * 4, 20)
    for i in range(0, len(todo), chunk):
        if should_stop():
            failed.extend(todo[i:])
            break
        part = todo[i:i + chunk]
        with ThreadPoolExecutor(max_workers=threads) as ex:
            for t, real in zip(part, ex.map(worker, part)):
                if real and "news.google" not in real:
                    t2 = dict(t)
                    t2["decoded_from"] = t2["url"]
                    t2["url"] = real
                    resolved.append(t2)
                else:
                    failed.append(t)
        log(f"      title rescue: {min(i + chunk, len(todo))}/{len(todo)} "
            f"· 🛟 {len(resolved)} recovered")
    if resolved:
        log(f"   🛟 {len(resolved)} clips recovered via title search — "
            f"no Google decoding needed.")
    return resolved, failed
