"""Run the team's Social Scraper with settings from the dashboard (the same steps as pressing "Scrape" in Colab)."""
from __future__ import annotations

import contextlib
import io
import os
import threading
import time

from . import settings

MODES = {"keyword": None, "link": "BY LINK (paste URLs)", "profile": "BY PROFILE (paste profile URLs)",
         "mixed": "MIXED (keyword + link + profil sekaligus)"}


def engine():
    from . import engine as eng          # imported on first use (pandas, requests)
    return eng


def platforms() -> list[str]:
    eng = engine()
    return ["ALL platforms"] + list(eng.PLATFORMS.keys()) + [eng.IG_KW_PLATFORM, eng.FB_PLATFORM, eng.TW_PLATFORM]


class _Lines(io.TextIOBase):
    """print() of the team's script -> one Activity Log line per line."""
    def __init__(self, log):
        self.log, self.buf = log, ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.log(line.rstrip())
        return len(s)

    def flush(self):
        if self.buf.strip():
            self.log(self.buf.rstrip())
        self.buf = ""


def _split_dt(value: str) -> tuple[list[str], list[str]]:
    """'2026-11-09 06:30' -> (['2026','11','09'], ['06','30']); the form takes minutes in steps of 5."""
    d, _, t = (value or "").strip().partition(" ")
    ymd = d.split("-") if d.count("-") == 2 else ["", "", ""]
    hm = t.split(":")[:2] if ":" in t else ["", ""]
    if hm[0]:
        hm = [f"{int(hm[0]):02d}", f"{(int(hm[1] or 0) // 5) * 5:02d}"]
    return ymd, hm


def run(cfg: dict, log, progress, stop_event: threading.Event, out_dir: str, data_dir: str) -> list[str]:
    """Fill the team's form from cfg and run it; returns the files written (Excel/CSV, in out_dir)."""
    eng = engine()
    keys = settings.load()
    if not keys.get("api_key"):
        raise RuntimeError("No API key set — an admin sets it under API settings.")
    eng.API_KEY = keys["api_key"]
    eng.HEADERS["x-api-key"] = keys["api_key"]
    eng.TWITTER_API_KEY = keys.get("twitter_api_key", "")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    eng.OUT_DIR = out_dir
    eng.CHECKPOINT_FILE = os.path.join(data_dir, "api_checkpoint_partial.csv")
    eng.SEEN_FILE = os.path.join(data_dir, "api_seen_ids.json")
    eng.STOP["on"] = False
    eng._prof_cache.clear()

    mode = cfg.get("mode", "keyword")
    plat = MODES.get(mode) or cfg.get("platform") or "ALL platforms"
    if plat not in platforms() + [m for m in MODES.values() if m]:
        raise RuntimeError(f"Unknown platform: {plat}")
    f = eng                                                        # the form widgets live in the engine module
    f.platform_w.value = plat
    f.queries_w.value = cfg.get("queries", "")
    f.limit_w.value = int(cfg.get("limit", 30))
    f.budget_w.value = int(cfg.get("budget", keys.get("default_budget", 200)))
    tf = bool(cfg.get("time_from") or cfg.get("time_to"))
    f.timefilter_w.value = tf
    (sy, smo, sd), (sh, smi) = _split_dt(cfg.get("time_from", ""))
    (uy, umo, ud), (uh, umi) = _split_dt(cfg.get("time_to", ""))
    for w, v in ((f.since_y, sy), (f.since_mo, smo), (f.since_da, sd), (f.since_h, sh), (f.since_mi, smi),
                 (f.until_y, uy), (f.until_mo, umo), (f.until_da, ud), (f.until_h, uh), (f.until_mi, umi)):
        w.value = v
    flags = {"dedup_w": True, "coverage_w": False, "delta_w": False, "comments_w": False, "cmt_kw_w": False,
             "foll_w": False, "fillid_w": True, "twrt_w": True, "twrt_link_w": False, "strict_w": True,
             "neg_use_w": False}
    for name, default in flags.items():
        getattr(f, name).value = bool(cfg.get(name[:-2], default))
    for name, default in {"cmt_top_w": 20, "cmt_max_w": 30, "twrt_cap_w": 0, "twrt_tot_w": 0}.items():
        getattr(f, name).value = int(cfg.get(name[:-2], default) or 0)
    f.cmt_kw_txt.value = cfg.get("cmt_kw_txt", "")
    f.lang_w.value = cfg.get("lang", "") if cfg.get("lang", "") in ("", "id", "en") else ""
    f.neg_kw_w.value = cfg.get("neg_kw", "")
    f.import_w.value = "\n".join(ln for ln in str(cfg.get("import", "")).splitlines()
                                 if not ln.strip().lower().endswith((".xlsx", ".xls", ".csv")))   # no server paths
    f.nlp_w.value = "lexicon"
    f.raw_w.value = False
    for cb in f.col_checks.values():
        cb.value = True
    f.fmt_w.value = "Excel + CSV"
    f.save_w.value = "Server"                                    # neither Drive nor download: the Exports folder

    from . import colab_shim

    def hook(w):
        if isinstance(w, colab_shim.IntProgress):
            progress(int(w.value or 0), int(getattr(w, "max", 0) or 0), None)
        elif isinstance(w, colab_shim.Label) and isinstance(w.value, str) and w.value:
            progress(None, None, w.value)
    colab_shim.HOOK["fn"] = hook

    def watch():
        while not done.is_set():
            if stop_event.is_set():
                eng.STOP["on"] = True
                return
            time.sleep(0.5)
    done = threading.Event()
    threading.Thread(target=watch, daemon=True).start()
    before = set(os.listdir(out_dir))
    out = _Lines(log)
    try:
        with contextlib.redirect_stdout(out):
            eng.on_click(None)
    finally:
        out.flush()
        done.set()
        colab_shim.HOOK["fn"] = None
    return sorted(n for n in os.listdir(out_dir) if n not in before and n.endswith((".xlsx", ".csv")))


def balance() -> dict:
    """The provider's credit balance for the saved API key (no scraping)."""
    keys = settings.load()
    if not keys.get("api_key"):
        return {"ok": False, "error": "No API key set."}
    eng = engine()
    eng.HEADERS["x-api-key"] = keys["api_key"]
    b = eng.check_credits()
    for k in ("creditCount", "credits", "credit_balance", "balance"):
        if isinstance(b, dict) and isinstance(b.get(k), (int, float)):
            return {"ok": True, "credits": int(b[k])}
    return {"ok": False, "error": str(b)[:200]}
