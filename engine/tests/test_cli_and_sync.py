"""CLI demo end-to-end + Supabase gating behaviour."""
from __future__ import annotations

import json
import threading

from stockopoly import settings, supabase_sync
from stockopoly.__main__ import main as cli_main
from stockopoly.store import open_conn
from tests.helpers import make_bundle_zip


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


def _ready(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "key")
    settings.put("share_supabase", True)
    settings.put("auto_sync", True)


def test_sync_status_reports_bucket_and_mode(monkeypatch):
    _ready(monkeypatch)
    st = supabase_sync.sync_status()
    assert st["mode"] == "ready"
    assert st["bucket"] == "stockopoly-photos"
    assert st["auto"] is True
    assert st["unsynced_photos"] == 0


def test_supabase_full_table_push_reuploads(monkeypatch):
    from stockopoly import imports
    _ready(monkeypatch)
    imports.import_parts([{"part": "P-1", "description": "x", "uom": "EA", "cost": 1.0}])
    sent: list[tuple[str, str, int]] = []
    monkeypatch.setattr(supabase_sync, "_push",
                        lambda url, key, table, conflict, rows:
                        sent.append((table, conflict, len(rows))) or True)
    assert supabase_sync.sync_now(["parts"])["pushed"] == 1
    assert sent == [("stockopoly_parts", "part_number", 1)]
    # "full" mode carries no watermark, so a second pass re-upserts the row
    sent.clear()
    assert supabase_sync.sync_now(["parts"])["pushed"] == 1


def test_photo_binaries_upload_once(monkeypatch, tmp_path):
    from stockopoly import intake
    _ready(monkeypatch)
    intake.ingest_bundle(make_bundle_zip(tmp_path, "sess-photo", n_photos=2))

    uploaded: list[str] = []
    pushed: list[tuple[str, int]] = []
    monkeypatch.setattr(supabase_sync, "_upload_object",
                        lambda url, key, bucket, obj, content, ctype:
                        uploaded.append(obj) or True)
    monkeypatch.setattr(supabase_sync, "_push",
                        lambda url, key, table, conflict, rows:
                        pushed.append((table, len(rows))) or True)

    out = supabase_sync.sync_now()
    assert out["mode"] == "ready" and out["photos"] == 2
    assert len(uploaded) == 2
    assert all(o.startswith("sess-photo/") for o in uploaded)
    assert ("stockopoly_photos", 1) in pushed

    cn = open_conn()
    try:
        synced = cn.execute(
            "SELECT COUNT(*) FROM photos WHERE synced_at IS NOT NULL").fetchone()[0]
        url = cn.execute(
            "SELECT remote_url FROM photos WHERE remote_url IS NOT NULL LIMIT 1"
        ).fetchone()[0]
    finally:
        cn.close()
    assert synced == 2
    assert url.startswith("https://example.supabase.co/storage/v1/object/public/"
                          "stockopoly-photos/")
    # idempotent: nothing left to upload on a second pass
    assert supabase_sync.sync_now()["photos"] == 0


def test_sync_async_gating_and_run(monkeypatch):
    # off switch wins even when configured
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "key")
    settings.put("share_supabase", False)
    assert supabase_sync.sync_async() is False
    # auto_sync off disables the automatic trigger only
    settings.put("share_supabase", True)
    settings.put("auto_sync", False)
    assert supabase_sync.sync_async() is False
    # unconfigured → no-op
    settings.put("auto_sync", True)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert supabase_sync.sync_async() is False
    # ready → runs on a background thread
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    ran = threading.Event()
    monkeypatch.setattr(supabase_sync, "sync_now", lambda *a, **k: ran.set())
    assert supabase_sync.sync_async() is True
    assert ran.wait(timeout=5)


def test_cli_sync_unconfigured(monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    settings.put("share_supabase", True)
    assert cli_main(["sync"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "unconfigured"
