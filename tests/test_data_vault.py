"""Data by year: counts per month, a month as an Excel ZIP or a full backup ZIP, and each ZIP back into an empty
server — nothing lost, nothing doubled."""
import os
import time
import zipfile
import zlib

import pytest


def _wait(fn, secs=60):
    t = time.time()
    while time.time() - t < secs:
        if fn():
            return True
        time.sleep(0.2)
    return False


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from crawler import data_vault, paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(paths, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(stream, "_STREAM", None)
    data_vault._STATE["overview"] = {"t": 0.0, "v": None, "busy": False}
    data_vault._STATE["build"] = {}
    return tmp_path


def _seed():
    from crawler import archive, stream
    s = stream.get()
    rows = []
    for i, day in enumerate(("2026-10-05", "2026-10-05", "2026-10-20", "2026-11-02")):
        rows.append((f"media{i}.id/berita/a{i}", f"https://media{i}.id/berita/artikel-ke-{i}", f"media{i}.id",
                     f"Judul berita nomor {i} tentang listrik", "sitemap", day, "10:1" + str(i), time.time(),
                     "success", time.time(), day, "10:1" + str(i), "Penulis", f"Ringkasan {i}",
                     zlib.compress(f"Isi berita nomor {i}. Paragraf kedua.".encode()), "", "budi", "budi, ani",
                     "listrik", "", "positive"))
    with s.lock:
        s._db.executemany("INSERT INTO articles(key, url, domain, title, source, feed_date, feed_time, discovered, "
                          "status, fetched, pub_date, pub_time, author, summary, content, image, influencer, persons, "
                          "matched, error, sentiment) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        s._db.commit()
    archive.add("medsos", [("pln", {"Platform": "TikTok", "Username": "warga1", "Text": "listrik padam lagi",
                                     "Date": "05-Oct-2026", "Time": "09:00:00", "Link": "https://tiktok.com/@w/1",
                                     "Sentiment": "negative", "Post ID": "1"})])
    archive.add("online", [("pln", {"DATE": "05-Oct-2026", "TIME": "08:00:00", "TITLE": "Hanya dari News Search",
                                     "URL": "https://lain.id/berita/hanya-dari-news-search", "MEDIA": "lain.id",
                                     "CONTENT": "<p>isi</p>", "SENTIMENT": "neutral"})])


def test_counts_per_year_and_month(env):
    from crawler import data_vault
    _seed()
    ov = data_vault.overview(wait=30)["overview"]
    y = ov["years"][0]
    assert y["year"] == "2026" and y["articles"] == 4
    oct_ = next(m for m in y["months"] if m["month"] == "2026-10")
    assert oct_["articles"] == 3 and oct_["medsos_rows"] == 1 and oct_["online_rows"] == 1 and oct_["bytes"] > 0


def _build(period, fmt):
    from crawler import data_vault
    from crawler.paths import OUTPUT_DIR
    out = data_vault.start_build(period, fmt)
    assert out["ok"]
    assert _wait(lambda: not data_vault._STATE["build"].get("running"))
    assert not data_vault._STATE["build"].get("error"), data_vault._STATE["build"]
    return os.path.join(OUTPUT_DIR, out["file"])


def test_excel_zip_has_online_and_medsos_files_per_day(env):
    _seed()
    path = _build("2026-10", "excel")
    names = zipfile.ZipFile(path).namelist()
    assert "2026-10/Online_2026-10-05.xlsx" in names and "2026-10/Online_2026-10-20.xlsx" in names
    assert "2026-10/Medsos_2026-10-05.xlsx" in names
    from openpyxl import load_workbook
    import io
    wb = load_workbook(io.BytesIO(zipfile.ZipFile(path).read("2026-10/Online_2026-10-05.xlsx")), read_only=True)
    urls = [r[3] for r in wb.worksheets[0].iter_rows(min_row=2, values_only=True)]
    assert len(urls) == 3 and "https://lain.id/berita/hanya-dari-news-search" in urls     # stream + News Search rows


def _fresh_server(env, monkeypatch, name):
    from crawler import data_vault, paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(env / name))
    monkeypatch.setattr(stream, "_STREAM", None)
    data_vault._STATE["overview"] = {"t": 0.0, "v": None, "busy": False}


def _restore(path):
    from crawler import cleanup
    from crawler.paths import APP_DATA
    up = os.path.join(APP_DATA, "uploads")
    os.makedirs(up, exist_ok=True)
    tmp = os.path.join(up, "abcdef0123456789.upload")
    with open(path, "rb") as f, open(tmp, "wb") as g:
        g.write(f.read())
    assert cleanup.start_restore(tmp)["ok"]
    assert _wait(lambda: not cleanup._STATE["restore"].get("running"))
    st = cleanup._STATE["restore"]
    assert not st.get("error"), st
    return st


def test_backup_zip_restores_everything_exactly(env, monkeypatch):
    from crawler import archive, stream
    _seed()
    before = {r["key"]: dict(r) for r in stream.get().rq("SELECT * FROM articles WHERE pub_date LIKE '2026-10%'")}
    path = _build("2026", "backup")
    _fresh_server(env, monkeypatch, "server2")
    st = _restore(path)
    assert st["articles"] == 4 and st["report_rows"] == 2
    after = {r["key"]: dict(r) for r in stream.get().rq("SELECT * FROM articles WHERE pub_date LIKE '2026-10%'")}
    assert set(after) == set(before)
    for k, a in before.items():
        b = after[k]
        for col in ("url", "title", "pub_date", "pub_time", "author", "summary", "persons", "influencer", "matched",
                    "sentiment", "status"):
            assert a[col] == b[col], col
        assert zlib.decompress(a["content"]) == zlib.decompress(b["content"])
    c = archive._db()
    assert c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    c.close()
    st2 = _restore(path)                                            # again: nothing doubled
    assert st2["articles"] == 0 and st2["report_rows"] == 0


def test_excel_zip_restores_into_an_empty_server(env, monkeypatch):
    from crawler import stream
    _seed()
    path = _build("2026-10", "excel")
    _fresh_server(env, monkeypatch, "server3")
    st = _restore(path)
    assert st["files"] == 3 and st["files_done"] == 3
    keys = {r["url"] for r in stream.get().rq("SELECT url FROM articles WHERE status='success'")}
    assert "https://media0.id/berita/artikel-ke-0" in keys and "https://lain.id/berita/hanya-dari-news-search" in keys


def test_a_single_excel_still_restores_and_an_xlsx_is_not_taken_for_a_zip(env, monkeypatch):
    from crawler import data_vault
    _seed()
    path = _build("2026-10", "excel")
    one = os.path.join(str(env), "one.xlsx")
    with open(one, "wb") as f:
        f.write(zipfile.ZipFile(path).read("2026-10/Online_2026-10-20.xlsx"))
    assert not data_vault.is_zip_of_files(one) and data_vault.is_zip_of_files(path)
    _fresh_server(env, monkeypatch, "server4")
    assert _restore(one)["articles"] == 1


def test_no_zip_without_room_on_the_disk_and_bad_periods_refused(env, monkeypatch):
    from crawler import data_vault
    _seed()
    monkeypatch.setattr(data_vault, "_disk", lambda: {"free": 1024 ** 3, "total": 10 * 1024 ** 3})
    with pytest.raises(ValueError, match="Not enough free disk"):
        data_vault.start_build("2026", "excel")
    with pytest.raises(ValueError):
        data_vault.start_build("Oktober", "excel")
    with pytest.raises(ValueError):
        data_vault.start_build("2026-10", "pdf")


def test_a_file_in_another_layout_is_named_not_skipped_silently(env, monkeypatch):
    """A ZIP with one Excel in the team's template and one with other columns: the first is restored, the second is
    named as skipped (not the team's Online / Medsos template)."""
    from openpyxl import Workbook
    _seed()
    good = _build("2026-10", "excel")
    other = os.path.join(str(env), "lain.xlsx")
    wb = Workbook()
    wb.active.append(["Tanggal", "Judul", "Link"])
    wb.active.append(["2026-10-05", "Berita", "https://contoh.id/berita/a"])
    wb.save(other)
    mixed = os.path.join(str(env), "campur.zip")
    with zipfile.ZipFile(mixed, "w") as z, zipfile.ZipFile(good) as g:
        z.writestr("Oktober/Online_2026-10-20.xlsx", g.read("2026-10/Online_2026-10-20.xlsx"))
        z.write(other, "Oktober/format_lain.xlsx")
    _fresh_server(env, monkeypatch, "server5")
    st = _restore(mixed)
    assert st["articles"] == 1 and st["skipped"] == ["Oktober/format_lain.xlsx"]
