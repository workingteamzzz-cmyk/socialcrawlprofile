"""Home dashboard: trend, sentiment, top media / keywords and latest items from the report database."""
import os
import sys
from datetime import date, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WIB = timezone(timedelta(hours=7))


def test_dashboard_numbers(tmp_path, monkeypatch):
    from crawler import archive, dashboard, paths
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    today = date(2026, 11, 20)
    d = lambda n: (today - timedelta(days=n)).isoformat()  # noqa: E731
    archive.add("online", [("banjir", {"DATE": d(0), "TIME": "08:00:00", "TITLE": "Banjir Bekasi", "URL": "https://detik.com/a",
                                       "MEDIA": "detik.com", "SENTIMENT": "negative"}),
                           ("banjir, bendungan", {"DATE": d(3), "TITLE": "Bendungan", "URL": "https://kompas.com/b",
                                                  "MEDIA": "kompas.com", "SENTIMENT": "positive"}),
                           ("banjir", {"DATE": d(10), "TITLE": "Lama", "URL": "https://detik.com/old", "MEDIA": "detik.com",
                                       "SENTIMENT": "neutral"})])
    archive.add("medsos", [("banjir", {"Sentiment": "Negative", "Username": "warga", "Text": "banjir lagi", "Date": d(1),
                                       "Url": "https://x.com/warga/status/1", "Platform": "X", "Post ID": "1"})])
    r = dashboard.build(7, today=today)
    assert r["start"] == d(6) and r["end"] == d(0) and len(r["trend"]) == 7
    assert r["trend"][-1]["online"] == 1 and r["trend"][-2]["medsos"] == 1
    assert r["kpi"]["articles"] == 2 and r["kpi"]["articles_prev"] == 1 and r["kpi"]["posts"] == 1
    assert r["kpi"]["negative_pct"] == 67 and r["kpi"]["media"] == 2
    assert r["sentiment"]["medsos"]["negative"] == 1
    assert r["top_keywords"][0] == ("banjir", 3) and ("x", 1) not in r["top_platforms"]
    assert r["latest"]["online"][0]["title"] == "Banjir Bekasi" and r["latest"]["medsos"][0]["source"] == "@warga"
    q = dashboard.build(7, "bendungan", today=today)
    assert q["kpi"]["articles"] == 1 and q["latest"]["online"][0]["source"] == "kompas.com"


def test_dashboard_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from tests.test_users import _app, _client
    app, _ = _app(tmp_path, monkeypatch)
    r = _client(app, "citra").get("/api/dashboard?days=30&q=pln").json()
    assert r["days"] == 30 and len(r["trend"]) == 30 and r["q"] == "pln"


def test_home_numbers_are_kept_for_a_minute_and_counted_again_after(tmp_path, monkeypatch):
    """The same Home view within a minute comes from memory (no second read of the archive); later it is fresh."""
    from datetime import date
    from crawler import archive, dashboard, paths
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    dashboard._CACHE.clear()
    today = date(2026, 11, 22)
    a = dashboard.build(7, today=today)
    archive.add("online", [("pln", {"DATE": "22-Nov-2026", "TITLE": "Baru", "URL": "https://x.id/berita/baru",
                                     "MEDIA": "x.id", "SENTIMENT": "negative"})])
    assert dashboard.build(7, today=today)["kpi"] == a["kpi"]                 # within the minute: as it was
    monkeypatch.setattr(dashboard.time, "time", lambda: a["generated"] + 61)
    assert dashboard.build(7, today=today)["kpi"]["articles"] == a["kpi"]["articles"] + 1
