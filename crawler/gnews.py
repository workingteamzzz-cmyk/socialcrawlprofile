"""Google News link decoder that runs on the server itself (no browser) — the same two steps as the team's notebook:
read the article's signature from news.google.com, then ask Google's own "batchexecute" for the media's link.

Used (1) as a second pass at the end of every news search, for links the browser could not decode, and
(2) by the "Fix Google News links in a file" tool. Only the article id is sent, only to news.google.com.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
GN_RE = re.compile(r"https?://news\.google\.com/(?:rss/)?(?:articles|read)/([A-Za-z0-9_\-]+)")
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_cache: dict[str, str] = {}
_cache_lock = threading.Lock()


def is_gnews(url) -> bool:
    if not isinstance(url, str):
        return False
    u = url.strip()
    return (urllib.parse.urlparse(u).hostname or "") == "news.google.com" and GN_RE.search(u) is not None


def article_id(url: str) -> str:
    m = GN_RE.search(url or "")
    return m.group(1) if m else ""


class Decoder:
    """Decode with a cache, a pause between requests and retries (longer waits when Google says "too many")."""

    def __init__(self, delay: float = 1.0, retries: int = 3, stop=None, sleep=time.sleep):
        self.delay, self.retries, self.stop, self.sleep = delay, retries, stop, sleep
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self._last = 0.0

    def _http(self, url, data=None, headers=None, timeout=20) -> str:
        h = {"User-Agent": UA, "Cookie": "CONSENT=YES+cb", "Accept-Language": "id,en;q=0.8"}
        h.update(headers or {})
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            self.sleep(wait)
        self._last = time.time()
        req = urllib.request.Request(url, data=data, headers=h)
        with self.opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")

    def _signature(self, gid):
        last = None
        for base in ("https://news.google.com/rss/articles/", "https://news.google.com/articles/"):
            try:
                html = self._http(base + gid)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    raise                                   # "too many": the caller waits before trying again
                last = e
                continue
            except Exception as e:  # noqa: BLE001  (one form may redirect somewhere unreachable: try the other)
                last = e
                continue
            sg = re.search(r'data-n-a-sg="([^"]+)"', html)
            ts = re.search(r'data-n-a-ts="([^"]+)"', html)
            if sg and ts:
                return sg.group(1), ts.group(1)
        raise RuntimeError(f"no signature on Google's page ({last})" if last else "no signature on Google's page")

    def _online(self, gid) -> str:
        from .news import _parse_batchexecute
        sig, ts = self._signature(gid)
        inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None,
                                            None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                            gid, int(ts), sig])
        body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
        text = self._http(BATCH_URL, data=body.encode(), headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "Referer": "https://news.google.com/"})
        url = _parse_batchexecute(text)
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            raise RuntimeError("Google's answer had no link")
        return url

    def decode(self, url: str) -> tuple[str, str]:
        """(media link, "OK") or (the original link, reason)."""
        from .news import decode_gnews_offline
        gid = article_id(url)
        if not gid:
            return url, "not a Google News link"
        with _cache_lock:
            if gid in _cache:
                return _cache[gid], "OK"
        real = decode_gnews_offline(url)
        err = ""
        for attempt in range(self.retries + 1):
            if real:
                break
            if self.stop is not None and self.stop.is_set():
                return url, "stopped"
            try:
                real = self._online(gid)
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}"
                if attempt < self.retries:
                    self.sleep(30 * (attempt + 1) if e.code == 429 else 3 * (attempt + 1))   # "too many": wait longer
            except Exception as e:  # noqa: BLE001
                err = str(e)[:100] or e.__class__.__name__
                if attempt < self.retries:
                    self.sleep(3 * (attempt + 1))
        if not real:
            return url, err or "could not decode"
        with _cache_lock:
            if len(_cache) > 20000:
                _cache.clear()
            _cache[gid] = real
        return real, "OK"


# ---------- fix the links in a file (xlsx / csv / txt) ----------
def fix_file(src: str, out: str, log=print, stop=None, progress=None, decoder: Decoder | None = None) -> tuple[int, int]:
    """Copy src to out with every Google News link replaced by the media's link; xlsx files get a "Decode Log"
    sheet. Returns (decoded, failed)."""
    import csv
    dec = decoder or Decoder(stop=stop)
    ext = src.rsplit(".", 1)[-1].lower()
    entries = []                                  # [where, original, new or "", status]
    if ext in ("xlsx", "xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(src)
        targets = [(ws, c) for ws in wb.worksheets if ws.title != "Decode Log"
                   for row in ws.iter_rows() for c in row if is_gnews(c.value)]
        log(f"Found {len(targets)} Google News links in {len(wb.worksheets)} sheet(s)")
        for i, (ws, cell) in enumerate(targets, 1):
            orig = cell.value.strip()
            new, status = dec.decode(orig)
            if status == "OK":
                cell.value = new
                cell.hyperlink = new
            entries.append([f"{ws.title}!{cell.coordinate}", orig, new if status == "OK" else "", status])
            if progress:
                progress(i, len(targets))
            if status == "stopped":
                break
        if "Decode Log" in wb.sheetnames:
            del wb["Decode Log"]
        lg = wb.create_sheet("Decode Log")
        lg.append(["Cell", "Google News Link", "Original Link", "Status"])
        for e in entries:
            lg.append(e)
        for col, w in zip("ABCD", (16, 60, 80, 24)):
            lg.column_dimensions[col].width = w
        wb.save(out)
    else:
        with open(src, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f)) if ext == "csv" else [[ln.rstrip("\r\n")] for ln in f]
        cells = [(r, c) for r, row in enumerate(rows) for c, v in enumerate(row) if is_gnews(v)]
        log(f"Found {len(cells)} Google News links")
        for i, (r, c) in enumerate(cells, 1):
            orig = rows[r][c].strip()
            new, status = dec.decode(orig)
            if status == "OK":
                rows[r][c] = new
            entries.append([f"R{r + 1}C{c + 1}", orig, new if status == "OK" else "", status])
            if progress:
                progress(i, len(cells))
            if status == "stopped":
                break
        with open(out, "w", encoding="utf-8-sig" if ext == "csv" else "utf-8", newline="") as f:
            if ext == "csv":
                csv.writer(f).writerows(rows)
            else:
                f.write("\n".join(r[0] for r in rows) + "\n")
    ok = sum(1 for e in entries if e[3] == "OK")
    return ok, len(entries) - ok
