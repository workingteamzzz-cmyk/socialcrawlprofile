"""The one-time compaction: refuses while the database is in use, keeps every row, frees the empty space, keeps the
old file until it is removed on purpose."""
import os
import sqlite3

import pytest


@pytest.fixture()
def folder(tmp_path, monkeypatch):
    from crawler import paths
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path))
    db = tmp_path / "stream.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE articles(key TEXT PRIMARY KEY, body TEXT)")
    c.execute("CREATE INDEX a_b ON articles(body)")
    c.executemany("INSERT INTO articles VALUES (?,?)", [(f"k{i}", "x" * 500) for i in range(20000)])
    c.commit()
    c.execute("DELETE FROM articles WHERE rowid % 2 = 0")       # leaves empty space
    c.commit()
    c.close()
    return str(tmp_path)


def test_compact_keeps_every_row_and_shrinks(folder):
    from crawler import compact
    db = os.path.join(folder, "stream.db")
    before = os.path.getsize(db)
    compact.compact(folder)
    assert os.path.getsize(db) < before * 0.7
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 10000
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()
    assert os.path.exists(db + ".before-compact")
    with pytest.raises(SystemExit, match="earlier run"):           # not twice before the old file is removed
        compact.compact(folder)
    compact.remove_old(folder)
    assert not os.path.exists(db + ".before-compact")


def test_compact_refuses_while_the_database_is_in_use(folder):
    from crawler import compact
    db = os.path.join(folder, "stream.db")
    busy = sqlite3.connect(db)
    busy.execute("BEGIN IMMEDIATE")
    busy.execute("INSERT INTO articles VALUES ('new', 'y')")
    try:
        with pytest.raises(SystemExit, match="in use"):
            compact.compact(folder)
    finally:
        busy.rollback()
        busy.close()
    assert not os.path.exists(db + ".before-compact") and not os.path.exists(os.path.join(folder, "stream.compact.db"))


def test_only_the_journals_emptied(folder):
    from crawler import compact
    db = os.path.join(folder, "stream.db")
    c = sqlite3.connect(db)
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.executemany("INSERT INTO articles VALUES (?,?)", [(f"n{i}", "y" * 500) for i in range(3000)])
    c.commit()
    assert os.path.getsize(db + "-wal") > 1_000_000
    c.close()
    compact.journals(folder)
    assert not os.path.exists(db + "-wal") or os.path.getsize(db + "-wal") == 0
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 13000
    c.close()
