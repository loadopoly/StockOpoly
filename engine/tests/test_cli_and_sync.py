"""CLI demo end-to-end + Supabase gating behaviour."""
from __future__ import annotations

import json

from stockopoly import settings, supabase_sync
from stockopoly.__main__ import main as cli_main
from stockopoly.store import open_conn


def test_cli_init_and_status(capsys):
    assert cli_main(["init"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True

    assert cli_main(["status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["locations"] == 0


def test_cli_demo_builds_full_warehouse(capsys):
    assert cli_main(["demo"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["locations"] == 96
    assert out["parts"] == 24
    # the tape-measure chain grounded bay width near 48/480·1080 = 108"
    assert 100 < out["bay_width_solved_in"] < 116
    assert out["plan"]["parts_assigned"] > 0
    assert out["migration"]["moves"] > 0
    assert out["safety_stock"]["parts"] > 0

    cn = open_conn()
    try:
        n_tasks = cn.execute("SELECT COUNT(*) FROM move_tasks").fetchone()[0]
        n_vel = cn.execute("SELECT COUNT(*) FROM velocity").fetchone()[0]
    finally:
        cn.close()
    assert n_tasks == out["migration"]["moves"]
    assert n_vel == 24


def test_supabase_off_switch_wins(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "key")
    settings.put("share_supabase", False)
    assert supabase_sync.sync_status()["mode"] == "off"
    assert supabase_sync.sync_now() == {"mode": "off", "pushed": 0}


def test_supabase_unconfigured_noop(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    settings.put("share_supabase", True)
    st = supabase_sync.sync_status()
    assert st["enabled"] is True and st["configured"] is False
    assert supabase_sync.sync_now()["mode"] == "unconfigured"


def test_supabase_incremental_push(monkeypatch):
    from stockopoly import events
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "key")
    settings.put("share_supabase", True)
    events.record("demo_event", {"n": 1})
    events.record("demo_event", {"n": 2})

    sent: list[tuple[str, int]] = []
    monkeypatch.setattr(supabase_sync, "_push",
                        lambda url, key, table, conflict, rows:
                        sent.append((table, len(rows))) or True)
    out = supabase_sync.sync_now(["events"])
    assert out["mode"] == "ready" and out["pushed"] == 2
    assert sent == [("stockopoly_events", 2)]
    # watermark advanced → nothing left to push
    assert supabase_sync.sync_now(["events"])["pushed"] == 0
