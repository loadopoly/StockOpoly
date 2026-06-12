from __future__ import annotations

import os
from pathlib import Path

from stockopoly import kv, settings
from stockopoly.store import db_path, init_schema, open_conn


def test_db_path_honours_env(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere.sqlite"
    monkeypatch.setenv("STOCKOPOLY_DB_PATH", str(target))
    assert db_path() == target.resolve()


def test_schema_idempotent_and_wal():
    init_schema()
    init_schema()  # second run must be a no-op
    cn = open_conn()
    try:
        mode = cn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        tables = {r[0] for r in cn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for required in ("settings", "kv", "batches", "photos", "photo_groups",
                         "dim_entities", "dim_variables", "dim_measurements",
                         "locations", "parts", "inventory", "velocity",
                         "slotting_plans", "move_tasks", "ss_params", "scb_outbox"):
            assert required in tables, required
    finally:
        cn.close()


def test_kv_roundtrip():
    kv.kv_set("a:1", "hello")
    assert kv.kv_get("a:1") == "hello"
    kv.kv_set_json("a:2", {"x": 1})
    assert kv.kv_get_json("a:2") == {"x": 1}
    assert kv.kv_get_json("missing", default=42) == 42
    kv.kv_set("a:3", "z")
    assert [k for k, _ in kv.kv_scan_prefix("a:")] == ["a:1", "a:2", "a:3"]
    kv.kv_delete("a:1")
    assert kv.kv_get("a:1") is None


def test_settings_defaults_and_put():
    assert settings.get("fill_factor") == 0.85
    settings.put("fill_factor", 0.9)
    assert settings.get("fill_factor") == 0.9
    merged = settings.all_settings()
    assert merged["fill_factor"] == 0.9
    assert merged["dock_code"] == "DOCK"
    try:
        settings.get("nope")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


def test_settings_update_ignores_unknown():
    out = settings.update({"daily_move_budget": 10, "bogus_key": 1})
    assert out["daily_move_budget"] == 10
    assert "bogus_key" not in out
