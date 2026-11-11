"""DuckDuckGo (optional): a third net for outlets missed by Google/Bing."""

from datetime import datetime


def _timelimit(start):
    if not start:
        return None
    try:
        days = (datetime.now() - datetime.strptime(start, "%Y-%m-%d")).days
    except ValueError:
        return None
    if days <= 1:
        return "d"
    if days <= 7:
        return "w"
    if days <= 31:
        return "m"
    return "y"


def harvest(keyword, start=None, limit=None, log=print):
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            log("   ⚠️ ddgs is not installed — DuckDuckGo skipped.")
            return []
    tl = _timelimit(start)
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(keyword, region="id-id", timelimit=tl,
                               max_results=limit or None):
                u = r.get("url") or r.get("href") or ""
                if u.startswith("http"):
                    results.append({"url": u, "title": r.get("title", ""),
                                    "date": str(r.get("date", ""))[:10],
                                    "source": "duckduckgo", "domain": ""})
            for r in ddgs.text(keyword, region="id-id", timelimit=tl,
                               max_results=limit or None):
                u = r.get("href") or r.get("url") or ""
                if u.startswith("http"):
                    results.append({"url": u, "title": r.get("title", ""),
                                    "date": "", "source": "duckduckgo", "domain": ""})
    except Exception as e:
        log(f"   ⚠️ DuckDuckGo error ({type(e).__name__}) — results may be incomplete.")
    if results:
        log(f"   🦆 DuckDuckGo: {len(results)} results")
    return results
