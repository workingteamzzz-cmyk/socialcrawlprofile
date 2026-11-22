"""Free up space by period: back up (a Backup ZIP is required) -> delete (never read again) -> restore from the
Excel or the ZIP."""
import os
import sys
import time
import zlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def wait(cleanup, part):
    t = time.time()
    while cleanup.status()[part].get("running") and time.time() - t < 30:
        time.sleep(0.05)
    return cleanup.status()[part]


def test_backup_delete_restore(tmp_path, monkeypatch):
    from datetime import timedelta, timezone
    from crawler import archive, cleanup, insights, paths, server, stream
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(paths, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(server, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(stream, "_STREAM", None)
    for k in ("export", "delete", "restore"):
        cleanup._STATE[k] = {}
    s = stream.get()
    now = time.time() - 300                       # read a while ago: before the backup below began
    rows = []
    for i, day in enumerate(["2026-10-05", "2026-10-20", "2026-11-10"]):
        t = f"Banjir besar melanda kota nomor {i}"
        rows.append((f"a.id/berita/banjir-{i}", f"https://a.id/berita/banjir-{i}", "a.id", t, "success", now, now, day, "08:15",
                     t + ".", zlib.compress(f"{t}.\n\nParagraf kedua {i}.".encode()), "negative", "Rina", "Ahmad Sutrisno"))
    with s.lock:
        s._db.executemany("INSERT INTO articles(key, url, domain, title, status, discovered, fetched, pub_date, pub_time, "
                          "summary, content, sentiment, author, persons) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        s._db.commit()
    insights.index_new()
    archive.add_rows("online", [{"url": r[1], "title": r[3], "published_full": f"{r[7]} 08:15", "text": r[3]} for r in rows],
                     timezone(timedelta(hours=7)))
    (out / "20261012_Online_Auto_08.00.xlsx").write_bytes(b"x")
    os.utime(out / "20261012_Online_Auto_08.00.xlsx", (time.mktime((2026, 10, 12, 8, 0, 0, 0, 0, -1)),) * 2)
    p = cleanup.preview("2026-10-01", "2026-10-31")
    assert p["articles"] == 2 and p["report_rows"] == 2 and p["files"] == 1 and p["readable"] == 2
    assert cleanup.start_export("2026-10-01", "2026-10-31")["ok"]
    ex = wait(cleanup, "export")
    assert ex["rows"] == 2 and (out / ex["file"]).exists()
    r = cleanup.start_delete("2026-10-01", "2026-10-31")
    assert not r["ok"] and "Backup ZIP" in r["error"]                   # one Excel is not a backup: refused
    from crawler import data_vault
    data_vault._STATE["overview"] = {"t": 0.0, "v": None, "busy": False}
    assert data_vault.start_build("2026-10", "backup")["ok"]
    t = time.time()
    while data_vault._STATE["build"].get("running") and time.time() - t < 30:
        time.sleep(0.05)
    assert not data_vault._STATE["build"].get("error"), data_vault._STATE["build"]
    assert cleanup.start_delete("2026-10-01", "2026-10-31")["ok"]
    de = wait(cleanup, "delete")
    assert de["articles"] == 2 and de["report_rows"] == 2 and de["files"] == 1 and (out / ex["file"]).exists()
    assert s.q("SELECT COUNT(*) n FROM articles")[0]["n"] == 1 and insights.search("banjir")["total"] == 1
    assert s.add_found([{"url": "https://a.id/berita/banjir-0", "domain": "a.id"}], "rss") == 0   # never read again
    data = (out / ex["file"]).read_bytes()
    uid = "ab12cd34ef"
    half = len(data) // 2
    assert cleanup.upload_chunk(uid, 0, data[:half], False)["received"] == half
    with pytest.raises(ValueError):
        cleanup.upload_chunk(uid, 5, data[half:], True)                   # out of order
    assert cleanup.upload_chunk(uid, half, data[half:], True)["ok"]
    rs = wait(cleanup, "restore")
    assert rs["articles"] == 2 and rs["report_rows"] == 2 and not rs["error"]
    r = s.q("SELECT * FROM articles WHERE pub_date='2026-10-05'")[0]
    assert r["title"].startswith("Banjir") and "Paragraf kedua 0" in zlib.decompress(r["content"]).decode()
    insights.index_new()
    assert insights.search("banjir")["total"] == 3
    s.stop()


def test_endpoints_admin_only(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from crawler import paths, stream
    from tests.test_users import SITE, _app, _client
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(stream, "_STREAM", None)
    app, _ = _app(tmp_path, monkeypatch)
    op, admin = _client(app, "budi"), _client(app, "ana")
    assert op.post("/api/cleanup/delete", json={"start": "2026-10-01", "end": "2026-10-31"}, headers=SITE).status_code == 403
    assert admin.get("/api/cleanup", params={"start": "2026-10-01", "end": "2026-10-31"}).json()["preview"]["articles"] == 0
    r = admin.post("/api/restore/chunk?id=abcdef1234&offset=0&final=0", content=b"PK", headers={**SITE, "Content-Type": "application/octet-stream"})
    assert r.json()["received"] == 2
    stream.get().stop()
