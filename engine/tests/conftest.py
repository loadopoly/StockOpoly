"""Shared fixtures. Every test runs against a temp DB — never the real one."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # engine/ on sys.path


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    """Force DB + data dir to per-test temp paths and init the schema."""
    db = tmp_path / "stockopoly_test.sqlite"
    monkeypatch.setenv("STOCKOPOLY_DB_PATH", str(db))
    monkeypatch.setenv("STOCKOPOLY_DATA_DIR", str(tmp_path / "data"))
    # Keep tests hermetic from a developer's real sibling checkouts:
    monkeypatch.setenv("SCB_REPO_DIR", str(tmp_path / "no-such-scb"))
    monkeypatch.delenv("SCB_DB_PATH", raising=False)
    from stockopoly.store import init_schema
    init_schema()
    yield db


@pytest.fixture()
def fake_scb(tmp_path, monkeypatch):
    """A fake Supply-Chain-Brain sibling with an empty pipeline dir."""
    scb = tmp_path / "Supply-Chain-Brain"
    (scb / "pipeline").mkdir(parents=True)
    monkeypatch.setenv("SCB_REPO_DIR", str(scb))
    monkeypatch.delenv("SCB_DB_PATH", raising=False)
    return scb
