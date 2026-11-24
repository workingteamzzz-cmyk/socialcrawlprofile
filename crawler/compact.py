"""Make the stream's database small again — run once while the app is stopped.

    sudo docker compose stop collector
    sudo docker compose run --rm --no-deps collector python -m crawler.compact
    sudo docker compose up -d collector

What it does, in order, and it stops at the first thing that is not right:
  1. refuses to run while the app (or anything else) is using the database;
  2. empties the journal (stream.db-wal) into the database and cuts it to zero;
  3. writes a compact copy (VACUUM INTO: the empty space left by deleted links is not copied);
  4. checks the copy: SQLite's integrity check, and the rows of every table counted in both — they must be equal;
  5. puts the copy in place; the old file is kept as stream.db.before-compact until you remove it
     (python -m crawler.compact --remove-old, once the app runs fine).
The search and report databases' journals are emptied too.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time


def _gb(p: str) -> str:
    return f"{os.path.getsize(p) / 1e9:.2f} GB" if os.path.exists(p) else "-"


def _tables(c: sqlite3.Connection) -> list[str]:
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def empty_journal(path: str) -> None:
    if not os.path.exists(path):
        return
    c = sqlite3.connect(path, timeout=60)
    try:
        c.execute("PRAGMA journal_size_limit=268435456")
        busy, _f, _d = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        c.close()
    if busy:
        raise SystemExit(f"{os.path.basename(path)}: its journal could not be emptied (still in use) — stop the app first.")
    print(f"  {os.path.basename(path)}: journal emptied (now {_gb(path + '-wal')}), database {_gb(path)}")


def compact(folder: str) -> None:
    db = os.path.join(folder, "stream.db")
    new = os.path.join(folder, "stream.compact.db")
    old = os.path.join(folder, "stream.db.before-compact")
    if not os.path.exists(db):
        raise SystemExit(f"No database at {db}.")
    if os.path.exists(old):
        raise SystemExit(f"{old} is still there from an earlier run — check the app, then remove it "
                         "(python -m crawler.compact --remove-old) and run this again.")
    print(f"Before: stream.db {_gb(db)}, journal {_gb(db + '-wal')}")
    c = sqlite3.connect(db, timeout=5)
    try:
        c.execute("BEGIN EXCLUSIVE")                 # nobody else may use it now
        c.execute("ROLLBACK")
    except sqlite3.OperationalError:
        raise SystemExit("The database is in use — stop the app first:  sudo docker compose stop collector")
    finally:
        c.close()
    t = time.time()
    print("1/4 Emptying the journals…")
    from . import archive
    os.environ["SQLITE_TMPDIR"] = folder           # SQLite's scratch files go to the data disk, not the small /tmp
    for p in (db, os.path.join(folder, "search.db"), os.path.join(archive._dir(), "archive.db")):
        empty_journal(p)
    c = sqlite3.connect(db, timeout=60)
    try:
        ps, pc, fl = (c.execute(f"PRAGMA {x}").fetchone()[0] for x in ("page_size", "page_count", "freelist_count"))
        print(f"   {ps * fl / 1e9:.1f} GB of {ps * pc / 1e9:.1f} GB is empty space")
        free = os.statvfs(folder)
        if free.f_bavail * free.f_frsize < ps * (pc - fl) * 1.2:
            raise SystemExit("Not enough free disk for the compact copy — free some space first.")
        if os.path.exists(new):
            os.remove(new)
        print("2/4 Writing the compact copy (a few minutes)…")
        c.execute("VACUUM INTO ?", (new,))
        counts = {t_: c.execute(f'SELECT COUNT(*) FROM "{t_}"').fetchone()[0] for t_ in _tables(c)}
    finally:
        c.close()
    print("3/4 Checking the copy…")
    k = sqlite3.connect(new)
    try:
        ok = k.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise SystemExit(f"The copy is not sound ({ok}) — nothing was changed; {new} can be removed.")
        got = {t_: k.execute(f'SELECT COUNT(*) FROM "{t_}"').fetchone()[0] for t_ in _tables(k)}
        k.execute("PRAGMA journal_mode=WAL")
    finally:
        k.close()
    if got != counts:
        diff = {t_: (counts.get(t_), got.get(t_)) for t_ in set(counts) | set(got) if counts.get(t_) != got.get(t_)}
        raise SystemExit(f"Rows differ in the copy {diff} — nothing was changed; {new} can be removed.")
    print(f"   sound, every table's rows equal ({sum(counts.values()):,} rows)")
    print("4/4 Putting the copy in place…")
    os.replace(db, old)
    for ext in ("-wal", "-shm"):
        if os.path.exists(db + ext):
            os.remove(db + ext)
    os.replace(new, db)
    print(f"Done in {time.time() - t:.0f} s: stream.db {_gb(old)} -> {_gb(db)}.")
    print(f"The old file is kept as {old}. Start the app; when it runs fine, remove it with:")
    print("   sudo docker compose run --rm --no-deps collector python -m crawler.compact --remove-old")


def remove_old(folder: str) -> None:
    old = os.path.join(folder, "stream.db.before-compact")
    if os.path.exists(old):
        size = _gb(old)
        os.remove(old)
        print(f"Removed {old} ({size}).")
    else:
        print("Nothing to remove.")


def journals(folder: str) -> None:
    """Only the journals emptied into their databases (no compact copy) — minutes, with the app stopped:
        sudo docker compose stop collector
        sudo docker compose run --rm --no-deps collector python -m crawler.compact --journal
        sudo docker compose up -d collector"""
    from . import archive
    t = time.time()
    for p in (os.path.join(folder, "stream.db"), os.path.join(folder, "search.db"),
              os.path.join(archive._dir(), "archive.db")):
        print(f"{os.path.basename(p)}: journal {_gb(p + '-wal')} — emptying…")
        empty_journal(p)
    print(f"Done in {time.time() - t:.0f} s. Start the app again:  sudo docker compose up -d collector")


def main(argv: list[str]) -> None:
    from .stream import _dir
    if "--remove-old" in argv:
        remove_old(_dir())
    elif "--journal" in argv:
        journals(_dir())
    else:
        compact(_dir())


if __name__ == "__main__":
    main(sys.argv[1:])
