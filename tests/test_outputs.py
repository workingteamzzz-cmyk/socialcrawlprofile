"""Result files: all listed, kept until an admin deletes them; deleting is admin-only and stays inside the folder."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_list_and_delete(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from crawler import api, server
    from tests.test_users import SITE, _app, _client
    out = tmp_path / "out"
    out.mkdir()
    for n in ["Stream_All_20261120_2132.xlsx", "Search_banjir_20261120.xlsx", "Media_Access_Report_20261123.xlsx",
              "20261120_Online_Manual_24.00.xlsx", "20261120_Online_Manual_24.00.csv"] + [f"news_links_{i}.xlsx" for i in range(60)]:
        (out / n).write_bytes(b"x")
    (tmp_path / "secret.xlsx").write_bytes(b"keep me")
    monkeypatch.setattr(server, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(api, "OUTPUT_DIR", str(out), raising=False)
    app, _ = _app(tmp_path, monkeypatch)
    viewer, op, admin = _client(app, "citra"), _client(app, "budi"), _client(app, "ana")
    d = viewer.get("/api/outputs").json()
    names = {f["name"] for f in d["files"]}
    assert len(d["files"]) >= 64 and "Stream_All_20261120_2132.xlsx" in names and "Media_Access_Report_20261123.xlsx" in names
    assert not d["can_delete"] and admin.get("/api/outputs").json()["can_delete"]
    body = {"names": ["20261120_Online_Manual_24.00.xlsx", "../secret.xlsx", "/etc/passwd"]}
    assert op.post("/api/outputs/delete", json=body, headers=SITE).status_code == 403
    r = admin.post("/api/outputs/delete", json=body, headers=SITE).json()
    assert r["deleted"] == ["20261120_Online_Manual_24.00.xlsx"]
    assert not (out / "20261120_Online_Manual_24.00.csv").exists()          # its CSV twin goes too
    assert (tmp_path / "secret.xlsx").exists()                              # nothing outside the folder


def test_kept_forever_by_default(monkeypatch):
    from crawler import overview
    monkeypatch.delenv("RETENTION_DAYS", raising=False)
    checks = overview.build(False)["checks"]
    assert any(c["title"] == "Result files kept forever" for c in checks)
