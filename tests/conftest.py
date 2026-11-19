"""Every test gets its own safe-mode state (budgets, resting sites, Google block): tests open many fake
Google / platform pages and must neither use up nor depend on the budgets of a real install."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _own_safety_state(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("safety")
    from crawler import news, safety
    monkeypatch.setattr(safety, "_path", lambda: str(d / "safety.json"))
    monkeypatch.setattr(news, "_block_path", lambda: str(d / "google_block.json"))
    yield
