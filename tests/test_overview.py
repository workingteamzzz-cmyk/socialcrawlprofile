"""Home page: the checklist and today's numbers."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_overview_checks_and_numbers(tmp_path, monkeypatch):
    from datetime import timedelta, timezone
    from crawler import archive, overview, paths
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(paths, "LOGINS_FILE", str(tmp_path / "logins.json"))
    archive.add_rows("medsos", [{"platform": "x", "author": "a", "url": "https://x.com/a/status/1", "text": "pln",
                                 "ts": 1762780000}], timezone(timedelta(hours=7)))
    d = overview.build(False)
    assert d["total"] == len(d["checks"]) and 0 <= d["passed"] <= d["total"]
    assert all(c["state"] in ("ok", "warn", "bad") and c["title"] for c in d["checks"])
    assert d["checks"][0]["title"] == "Runs on this computer only"
    assert d["stats"]["posts_today"] == 1 and d["stats"]["articles_today"] == 0
    assert any(c["title"].startswith("Safe mode on") for c in d["checks"])
    assert not any(c["detail"].startswith(" ·") for c in d["checks"])


def test_overview_endpoint_needs_sign_in(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from tests.test_users import _app, _client
    app, _ = _app(tmp_path, monkeypatch)
    r = _client(app, "citra").get("/api/overview").json()
    assert r["checks"] and "stats" in r
