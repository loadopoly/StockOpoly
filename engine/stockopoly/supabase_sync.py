"""Optional Supabase sharing — OFF switch wins, missing config no-ops.

Pushes summary rows (events, batches, slotting plans, velocity) to Supabase
PostgREST tables named ``stockopoly_*`` so a team can watch progress from
anywhere. Three gates, in order:

1. the ``share_supabase`` setting — the user's OFF switch **fully disables**
   sync regardless of environment;
2. ``SUPABASE_URL`` + ``SUPABASE_SERVICE_KEY`` (or ``SUPABASE_ANON_KEY``)
   env vars — absent means status "unconfigured" and a clean no-op;
3. per-table watermarks in ``sync_state`` so pushes are incremental.

This module never raises to callers and shares nothing with the SCB
learning link, which stays always-on by design.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import settings
from .store import open_conn

logger = logging.getLogger(__name__)

_TABLES: dict[str, tuple[str, str, str]] = {
    # local table → (remote table, watermark column, conflict key)
    "events": ("stockopoly_events", "at", "at,kind"),
    "batches": ("stockopoly_batches", "created_at", "batch_id"),
    "slotting_plans": ("stockopoly_plans", "created_at", "plan_id"),
    "velocity": ("stockopoly_velocity", "computed_at", "part_number"),
}
_BATCH_LIMIT = 500


def _env() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "") or \
        os.environ.get("SUPABASE_ANON_KEY", "")
    return url, key


def sync_status() -> dict[str, Any]:
    enabled = bool(settings.get("share_supabase"))
    url, key = _env()
    state = {}
    try:
        cn = open_conn()
        try:
            state = {r["key"]: r["value"] for r in cn.execute(
                "SELECT key, value FROM sync_state")}
        finally:
            cn.close()
    except Exception:
        pass
    return {
        "enabled": enabled,
        "configured": bool(url and key),
        "url": url or None,
        "mode": "off" if not enabled else ("ready" if url and key else "unconfigured"),
        "watermarks": state,
    }


def _push(url: str, key: str, table: str, conflict: str, rows: list[dict]) -> bool:
    endpoint = f"{url}/rest/v1/{table}?on_conflict={conflict}"
    req = urllib.request.Request(
        endpoint, data=json.dumps(rows).encode("utf-8"), method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Supabase push to %s failed: %s", table, exc)
        return False


def sync_now(tables: list[str] | None = None) -> dict[str, Any]:
    """Incremental push of selected (default: all) summary tables."""
    status = sync_status()
    if not status["enabled"]:
        return {"mode": "off", "pushed": 0}
    url, key = _env()
    if not (url and key):
        return {"mode": "unconfigured", "pushed": 0}

    pushed = 0
    errors: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    cn = open_conn()
    try:
        for local, (remote, wm_col, conflict) in _TABLES.items():
            if tables and local not in tables:
                continue
            wm_key = f"supabase:{local}"
            row = cn.execute("SELECT value FROM sync_state WHERE key=?",
                             (wm_key,)).fetchone()
            watermark = row["value"] if row else ""
            rows = [dict(r) for r in cn.execute(
                f"SELECT * FROM {local} WHERE COALESCE({wm_col},'') > ?"
                f" ORDER BY {wm_col} LIMIT ?", (watermark, _BATCH_LIMIT))]
            if not rows:
                continue
            payload = [{**r, "node": "stockopoly"} for r in rows]
            if _push(url, key, remote, conflict, payload):
                pushed += len(rows)
                new_wm = max(str(r.get(wm_col) or "") for r in rows)
                cn.execute(
                    "INSERT INTO sync_state(key, value, updated_at) VALUES (?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                    " updated_at=excluded.updated_at", (wm_key, new_wm, now))
                cn.commit()
            else:
                errors.append(local)
    finally:
        cn.close()
    out: dict[str, Any] = {"mode": "ready", "pushed": pushed}
    if errors:
        out["errors"] = errors
    return out
