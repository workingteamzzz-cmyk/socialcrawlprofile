"""Search over every stream article, top stories, keyword spike alerts."""
import os
import sys
import time
import zlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def ins(tmp_path, monkeypatch):
    from crawler import insights, paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    insights._STATE["stories"] = (0.0, 0, None)
    s = stream.Stream(str(tmp_path / "data" / "stream"))
    now = time.time()
    arts = [  # one story told by 3 outlets, another by 2, singles, a keyword spike
        ("a.id", "Banjir Rendam Ratusan Rumah di Bekasi Timur", "banjir", now - 600),
        ("b.id", "Ratusan Rumah di Bekasi Timur Terendam Banjir", "banjir", now - 500),
        ("c.id", "Banjir Bekasi Timur: Ratusan Rumah Terendam Air", "banjir", now - 400),
        ("a.id", "Harga Cabai Rawit Melonjak Jelang Akhir Tahun", "", now - 300),
        ("d.id", "Jelang Akhir Tahun, Harga Cabai Rawit Melonjak", "", now - 200),
        ("e.id", "Timnas Indonesia Menang Telak di Laga Persahabatan", "", now - 100),
    ] + [(f"x{i}.id", f"Banjir susulan wilayah {i} hari ini", "banjir", now - 60) for i in range(4)] \
      + [("lama.id", "Banjir kecil kemarin", "banjir", now - 20 * 3600)]
    rows = [(f"{d}/{i}", f"https://{d}/berita/{i}-artikel", d, t, "success", now, f, "2026-11-20", "08:00",
             "Ringkasan: " + t, zlib.compress(("Isi lengkap. " + t + ". " * 40).encode()), m)
            for i, (d, t, m, f) in enumerate(arts)]
    with s.lock:
        s._db.executemany("INSERT INTO articles(key, url, domain, title, status, discovered, fetched, pub_date, "
                          "pub_time, summary, content, matched) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        s._db.commit()
    yield insights
    s.stop()


def test_query_syntax():
    from crawler.insights import fts_query
    assert fts_query('banjir "bekasi timur" -promo') == '(banjir "bekasi timur") NOT promo'
    assert fts_query("pln AND NOT iklan") == "pln NOT iklan"
    assert fts_query("(padam OR byarpet) pln*") == "( padam OR byarpet ) pln*"
    for bad in ("", "NOT banjir", "-promo"):
        with pytest.raises(ValueError):
            fts_query(bad)


def test_index_search_and_export(ins, tmp_path):
    import openpyxl
    assert ins.index_new() == 11 and ins.index_new() == 0                  # incremental
    r = ins.search('banjir "bekasi timur"')
    assert r["total"] == 3 and "<mark>" in r["rows"][0]["title"] and r["media"][0][1] == 1
    assert ins.search("banjir -bekasi")["total"] == 5
    assert ins.search("cabai OR timnas")["total"] == 3 and ins.search("melonjak", domain="d.id")["total"] == 1
    out = ins.export("banjir bekasi", 7, str(tmp_path / "out"))
    ws = openpyxl.load_workbook(tmp_path / "out" / out["file"]).active
    assert out["rows"] == 3 and ws.max_row == 4 and ws["K2"].value.startswith("<p>")
    ins.purge_index(0)
    assert ins.search("banjir")["total"] == 0


def test_top_stories(ins):
    st = ins.stories(6)
    top = st["stories"]
    by = {("Bekasi" if "Bekasi" in s["title"] else "Cabai" if "Cabai" in s["title"] else "susulan"): s for s in top}
    assert top[0]["outlets"] == 4 and by["susulan"] is top[0]                     # most outlets first
    assert by["Bekasi"]["outlets"] == 3 and by["Bekasi"]["keywords"] == ["banjir"] and by["Cabai"]["outlets"] == 2
    assert all(s["outlets"] >= 2 for s in top) and not any("Timnas" in s["title"] for s in top)


def test_spike_alert_once(ins, monkeypatch):
    from crawler import captcha
    sent = []
    monkeypatch.setattr(captcha, "notify", lambda text: sent.append(text))
    sp = ins.spikes()
    assert sp and sp[0]["keyword"] == "banjir" and sp[0]["last_hour"] == 7
    assert ins.alert_spikes() and "banjir" in sent[0]
    assert ins.alert_spikes() == [] and len(sent) == 1                       # quiet for 3 h
    ins.save_settings({"spike_min": 50})
    assert ins.spikes() == []


def test_endpoints(ins, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from tests.test_users import SITE, _app, _client
    ins.index_new()
    app, _ = _app(tmp_path, monkeypatch)
    viewer, op, admin = _client(app, "citra"), _client(app, "budi"), _client(app, "ana")
    r = viewer.get("/api/search", params={"q": "banjir bekasi"}).json()
    assert r["ok"] and r["total"] == 3
    assert viewer.get("/api/search", params={"q": "NOT x"}).json()["ok"] is False
    assert viewer.get("/api/stories?hours=6").json()["stories"]
    assert viewer.post("/api/search/export", json={"q": "banjir"}, headers=SITE).status_code == 403
    assert op.post("/api/insights", json={"spike_min": 9}, headers=SITE).status_code == 403
    assert admin.post("/api/insights", json={"spike_min": 9}, headers=SITE).json()["spike_min"] == 9
    assert viewer.get("/api/insights").json()["settings"]["spike_min"] == 9
