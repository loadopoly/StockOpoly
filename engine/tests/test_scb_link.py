from __future__ import annotations

import json
import sqlite3

from stockopoly import scb_link
from stockopoly.store import open_conn


def _scb_rows(db):
    cn = sqlite3.connect(str(db))
    try:
        return cn.execute(
            "SELECT logged_at, kind, title, detail, signal_strength FROM learning_log"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        cn.close()


def test_direct_write_into_fake_scb(fake_scb):
    ok = scb_link.log_learning("stockopoly_intake", "Batch x", {"photos": 5}, 0.6)
    assert ok is True
    rows = _scb_rows(fake_scb / "pipeline" / "local_brain.sqlite")
    assert len(rows) == 1
    assert rows[0][1] == "stockopoly_intake"
    assert json.loads(rows[0][3]) == {"photos": 5}
    assert rows[0][4] == 0.6


def test_outbox_fallback_when_scb_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SCB_REPO_DIR", str(tmp_path / "definitely-missing"))
    ok = scb_link.log_learning("stockopoly_crawl", "t", {"u": 1}, 0.4)
    assert ok is False
    cn = open_conn()
    try:
        pending = cn.execute(
            "SELECT kind, flushed FROM scb_outbox WHERE flushed=0").fetchall()
    finally:
        cn.close()
    assert [r["kind"] for r in pending] == ["stockopoly_crawl"]
    # JSONL mirror exists and is cloud_learning_queue-shaped
    jsonl = scb_link._outbox_jsonl()
    assert jsonl.exists()
    last = json.loads(jsonl.read_text().strip().splitlines()[-1])
    assert last["kind"] == "stockopoly_crawl"
    assert last["node"] == "stockopoly"
    assert "signal_strength" in last and "logged_at" in last


def test_flush_outbox_delivers_when_scb_appears(tmp_path, monkeypatch, fake_scb):
    # First: force a queued event by pointing at a missing repo
    monkeypatch.setenv("SCB_REPO_DIR", str(tmp_path / "missing"))
    assert scb_link.log_learning("stockopoly_grouping", "g", {"n": 2}) is False
    # Now SCB "comes online"
    monkeypatch.setenv("SCB_REPO_DIR", str(fake_scb))
    flushed = scb_link.flush_outbox()
    assert flushed == 1
    rows = _scb_rows(fake_scb / "pipeline" / "local_brain.sqlite")
    assert [r[1] for r in rows] == ["stockopoly_grouping"]
    st = scb_link.status()
    assert st["outbox_pending"] == 0


def test_status_shape():
    st = scb_link.status()
    for key in ("scb_repo", "scb_db", "scb_db_exists", "outbox_pending"):
        assert key in st
