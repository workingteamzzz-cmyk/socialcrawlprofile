"""Reporting database: runs are archived, topics group keywords, reports come out in the team's templates."""
import os
import sys
from datetime import timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import openpyxl  # noqa: E402

WIB = timezone(timedelta(hours=7))


@pytest.fixture
def arch(tmp_path, monkeypatch):
    from crawler import archive, paths
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    return archive


def test_topic_report_online_and_medsos(arch, tmp_path):
    news = [{"url": "https://www.detik.com/a/1", "title": "Pemadaman listrik di Bogor", "published_full": "2026-11-09 20:15",
             "text": "Warga kecewa pemadaman listrik. “Kami minta maaf,” ujar Ahmad Sutrisno.", "keyword": "listrik padam"},
            {"url": "https://kompas.com/b/2/", "title": "Tarif listrik naik", "published_full": "2026-11-10 08:00",
             "text": "PLN mengumumkan tarif baru.", "keyword": "tarif"},
            {"url": "https://x.id/c", "title": "Banjir Jakarta", "published_full": "2026-11-10 09:00",
             "text": "Banjir melanda Jakarta.", "keyword": "banjir"}]
    assert arch.add_rows("online", news, WIB) == 3
    assert arch.add_rows("online", news[:1], WIB) == 0                     # found again: merged, not doubled
    social = [{"platform": "tiktok", "author": "warga1", "url": "https://www.tiktok.com/@warga1/video/111",
               "text": "PLN padam lagi", "ts": 1762780000, "likes": 5, "keyword": "pln"},
              {"platform": "x", "author": "budi", "url": "https://x.com/budi/status/222", "text": "banjir lagi",
               "ts": 1762780000, "keyword": "banjir"}]
    assert arch.add_rows("medsos", social, WIB) == 2
    t = arch.save_topic("PLN", "pln, listrik padam, tarif listrik")
    assert arch.topics()[0]["keywords"] == ["pln", "listrik padam", "tarif listrik"]
    r = arch.report("online", "2026-11-01", "2026-11-30", t["keywords"], t["name"], str(tmp_path / "out"))
    assert r["total"] == 2                                                   # Bogor (keyword) + tarif (text)
    wb = openpyxl.load_workbook(tmp_path / "out" / r["file"])
    assert wb.sheetnames == ["Sheet1", "Summary"]
    head = [c.value for c in wb["Sheet1"][1]]
    assert head[:4] == ["DATE", "TIME", "TITLE", "URL"] and wb["Sheet1"].max_row == 3
    only_tag = arch.report("online", "2026-11-01", "2026-11-30", t["keywords"], "PLN", str(tmp_path / "out"),
                           in_text=False)
    assert only_tag["total"] == 1
    m = arch.report("medsos", "2025-11-01", "2026-12-31", ["pln"], "PLN", str(tmp_path / "out"))
    assert m["total"] == 1 and m["top_accounts"][0][0] == "warga1"
    head = [c.value for c in openpyxl.load_workbook(tmp_path / "out" / m["file"])["Sheet1"][1]]
    assert head[:3] == ["No", "Sentiment", "Username"]
    assert arch.stats()["online"]["items"] == 3


def test_import_past_files(arch, tmp_path):
    from crawler import templates
    out = tmp_path / "exports"
    out.mkdir()
    templates.write("online", str(out / "20261110_Online_Manual_24.00.xlsx"), str(out / "a.csv"),
                    [{"url": "https://a.id/1", "title": "Listrik padam", "published_full": "2026-11-10 10:00",
                      "text": "Listrik padam di Bekasi."}], WIB)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(templates.MEDSOS_COLUMNS[:24] + ["ID", "User ID Parent", "Query"])          # an older API-scraper file
    ws.append([1, "negative", "u1", "9", 10, "pln mati", "2026-11-10", "10:00:00", 1, 0, 0,
               "https://www.tiktok.com/@u1/video/5", "anger", "", "TikTok", "post", 50] + [""] * 7 + ["5", "9", "pln"])
    wb.save(out / "Data TikTok_x.xlsx")
    r = arch.import_folder(str(out))
    assert r["files"] == 2 and r["online"] == 1 and r["medsos"] == 1
    m = arch.query("medsos", "2026-11-01", "2026-11-30", ["pln"], in_text=False)
    assert m and m[0]["Post ID"] == "5"


def test_report_endpoints_and_roles(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from crawler import paths
    from tests.test_users import SITE, _app, _client
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    app, _ = _app(tmp_path, monkeypatch)
    op, viewer = _client(app, "budi"), _client(app, "citra")
    assert viewer.post("/api/report/topics", json={"name": "PLN", "keywords": "pln"}, headers=SITE).status_code == 403
    r = op.post("/api/report/topics", json={"name": "PLN", "keywords": "pln, listrik padam"}, headers=SITE).json()
    assert r["ok"] and viewer.get("/api/report/topics").json()["topics"][0]["name"] == "PLN"
    rep = viewer.post("/api/report", json={"kind": "online", "start": "2026-11-01", "end": "2026-11-30",
                                           "topic_id": r["topic"]["id"]}, headers=SITE).json()
    assert rep["ok"] and rep["total"] == 0
    assert op.post("/api/report/topics/delete", json={"id": r["topic"]["id"]}, headers=SITE).json()["ok"]


def test_hourly_only_new(arch):
    from crawler.schedule import clean, describe
    rows = [{"platform": "x", "author": "a", "url": "https://x.com/a/status/1", "text": "pln", "ts": 1762780000},
            {"platform": "x", "author": "b", "url": "https://x.com/b/status/2", "text": "pln", "ts": 1762780000}]
    arch.add_rows("medsos", rows[:1], WIB)                                 # delivered by the previous run
    assert [r["url"] for r in arch.only_new("medsos", rows, WIB)] == ["https://x.com/b/status/2"]
    s = clean({"kind": "keywords", "payload": {"keywords": ["pln"]}, "repeat": "hours", "hours": 1,
               "window": "today", "new_only": True})
    assert s["new_only"] and describe(s).startswith("every hour, only new results")


def test_hourly_only_new_survives_the_api(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from crawler import schedule
    from tests.test_users import SITE, _app, _client
    monkeypatch.setattr(schedule, "SCHEDULER", schedule.Scheduler(str(tmp_path / "s.json"), None))
    app, _ = _app(tmp_path, monkeypatch)
    op = _client(app, "budi")
    r = op.post("/api/schedules/save", json={"kind": "news", "payload": {"keywords": ["pln"]}, "repeat": "hours",
                                             "hours": 1, "window": "today", "new_only": True}, headers=SITE).json()
    assert r["ok"] and r["schedule"]["new_only"] is True
    assert "only new results" in op.get("/api/schedules").json()["schedules"][0]["describe"]
