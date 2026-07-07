"""Supabase mirror — automatic upload of structured data **and photos**.

When sharing is on and credentials exist, every mutation triggers a background
push so a team can watch the warehouse from anywhere (e.g. the StockOpoly app
hosted on GitHub Pages, reading the same Supabase project the Operate Console
uses). Two payloads travel up:

* **structured rows** → PostgREST tables named ``stockopoly_*`` (parts,
  locations, inventory, velocity, occupancy, plans, assignments, move tasks,
  safety stock, batches, groups, events);
* **photo binaries** → Supabase **Storage** bucket (default
  ``stockopoly-photos``), one object per captured photo, with a metadata row
  in ``stockopoly_photos`` carrying the public URL.

Three gates, in order, all must pass:

1. the ``share_supabase`` setting — the user's OFF switch **fully disables**
   sync regardless of environment;
2. ``SUPABASE_URL`` + ``SUPABASE_SERVICE_KEY`` (or ``SUPABASE_ANON_KEY``)
   env vars — absent means status "unconfigured" and a clean no-op;
3. per-table watermarks / per-photo ``synced_at`` so pushes are incremental.

This module never raises to callers and shares nothing with the SCB learning
link, which stays always-on by design. The DDL for the remote tables and the
Storage bucket ships at ``engine/sql/supabase_schema.sql``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import settings
from .store import data_dir, open_conn

logger = logging.getLogger(__name__)

# local table → (remote table, mode, key/watermark column, on_conflict cols)
#   mode "incremental" — push rows whose watermark column advanced since last run
#   mode "full"        — upsert every row each run (paged); covers tables that are
#                        rewritten in place and carry no monotonic timestamp
_TABLES: dict[str, tuple[str, str, str, str]] = {
    "events":             ("stockopoly_events",       "incremental", "at",            "node,id"),
    "batches":            ("stockopoly_batches",      "incremental", "created_at",    "batch_id"),
    "photo_groups":       ("stockopoly_groups",       "incremental", "created_at",    "group_id"),
    "slotting_plans":     ("stockopoly_plans",        "incremental", "created_at",    "plan_id"),
    "parts":              ("stockopoly_parts",        "full",        "part_number",   "part_number"),
    "locations":          ("stockopoly_locations",    "full",        "location_code", "location_code"),
    "inventory":          ("stockopoly_inventory",    "full",        "rowid",         "part_number,location_code"),
    "velocity":           ("stockopoly_velocity",     "full",        "part_number",   "part_number"),
    "occupancy":          ("stockopoly_occupancy",    "full",        "location_code", "location_code"),
    "assignments_future": ("stockopoly_assignments",  "full",        "rowid",         "plan_id,part_number,location_code"),
    "move_tasks":         ("stockopoly_move_tasks",   "full",        "task_id",       "node,task_id"),
    "ss_params":          ("stockopoly_safety_stock", "full",        "part_number",   "part_number"),
}
_PHOTOS_TABLE = "stockopoly_photos"
_NODE = "stockopoly"
_BATCH_LIMIT = 500

_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                  ".webp": "image/webp", ".heic": "image/heic", ".gif": "image/gif"}

# Background-sync coalescing: one in-flight run at a time; concurrent triggers
# collapse into the running pass (which re-queries, so it sees the latest data).
_sync_lock = threading.Lock()
_sync_running = False
_sync_pending = False


def _env() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "") or \
        os.environ.get("SUPABASE_ANON_KEY", "")
    return url, key


def sync_status() -> dict[str, Any]:
    enabled = bool(settings.get("share_supabase"))
    url, key = _env()
    state: dict[str, str] = {}
    unsynced_photos = 0
    try:
        cn = open_conn()
        try:
            state = {r["key"]: r["value"] for r in cn.execute(
                "SELECT key, value FROM sync_state")}
            unsynced_photos = cn.execute(
                "SELECT COUNT(*) FROM photos WHERE synced_at IS NULL").fetchone()[0]
        finally:
            cn.close()
    except Exception:
        pass
    return {
        "enabled": enabled,
        "auto": bool(settings.get("auto_sync")),
        "configured": bool(url and key),
        "url": url or None,
        "bucket": settings.get("supabase_bucket"),
        "mode": "off" if not enabled else ("ready" if url and key else "unconfigured"),
        "unsynced_photos": unsynced_photos,
        "watermarks": state,
    }


def _push(url: str, key: str, table: str, conflict: str, rows: list[dict]) -> bool:
    """Upsert ``rows`` into a PostgREST table; merge on the conflict columns."""
    endpoint = f"{url}/rest/v1/{table}?on_conflict={conflict}"
    req = urllib.request.Request(
        endpoint, data=json.dumps(rows, default=str).encode("utf-8"), method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Supabase push to %s failed: %s", table, exc)
        return False


def _quote_path(object_path: str) -> str:
    """Percent-encode each path segment (spaces, #, ?, non-ASCII) so the
    Storage key round-trips as a URL — matches the TS client's per-segment
    ``encodeURIComponent``. The stored object key stays the raw path."""
    return "/".join(urllib.parse.quote(seg, safe="") for seg in object_path.split("/"))


def _upload_object(url: str, key: str, bucket: str, object_path: str,
                   content: bytes, content_type: str) -> bool:
    """PUT a binary into Supabase Storage (upsert), mirroring the JS SDK call."""
    endpoint = f"{url}/storage/v1/object/{bucket}/{_quote_path(object_path)}"
    req = urllib.request.Request(
        endpoint, data=content, method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": content_type, "x-upsert": "true",
                 "cache-control": "max-age=3600"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        # 409 = object already exists without upsert honoured; treat as present.
        if exc.code == 409:
            return True
        logger.warning("Supabase storage upload of %s failed: %s", object_path, exc)
        return False
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Supabase storage upload of %s failed: %s", object_path, exc)
        return False


def _public_url(url: str, bucket: str, object_path: str) -> str:
    return f"{url}/storage/v1/object/public/{bucket}/{_quote_path(object_path)}"


def _object_path(batch_id: str, file: str) -> str:
    """Stable, traversal-free Storage key for a photo."""
    safe = "/".join(seg for seg in f"{batch_id}/{file}".split("/")
                    if seg not in ("", ".", ".."))
    return safe


def _sync_tables(cn, url: str, key: str, tables: list[str] | None) -> tuple[int, list[str]]:
    pushed, errors, now = 0, [], datetime.now(timezone.utc).isoformat()
    for local, (remote, mode, col, conflict) in _TABLES.items():
        if tables is not None and local not in tables:
            continue
        if mode == "incremental":
            wm_key = f"supabase:{local}"
            row = cn.execute("SELECT value FROM sync_state WHERE key=?",
                             (wm_key,)).fetchone()
            watermark = row["value"] if row else ""
            # Page to exhaustion so a >_BATCH_LIMIT backlog drains in one pass
            # rather than waiting for the next trigger. rowid is the ordering
            # tiebreaker for rows that share a watermark value.
            while True:
                rows = [dict(r) for r in cn.execute(
                    f"SELECT * FROM {local} WHERE COALESCE({col},'') > ?"
                    f" ORDER BY {col}, rowid LIMIT ?", (watermark, _BATCH_LIMIT))]
                if not rows:
                    break
                payload = [{**r, "node": _NODE} for r in rows]
                if not _push(url, key, remote, conflict, payload):
                    errors.append(local)
                    break
                pushed += len(rows)
                watermark = max(str(r.get(col) or "") for r in rows)
                cn.execute(
                    "INSERT INTO sync_state(key, value, updated_at) VALUES (?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                    " updated_at=excluded.updated_at", (wm_key, watermark, now))
                cn.commit()
                if len(rows) < _BATCH_LIMIT:
                    break
        else:  # full upsert, paged by rowid for stable ordering
            offset, ok = 0, True
            while True:
                rows = [dict(r) for r in cn.execute(
                    f"SELECT * FROM {local} ORDER BY rowid LIMIT ? OFFSET ?",
                    (_BATCH_LIMIT, offset))]
                if not rows:
                    break
                payload = [{**r, "node": _NODE} for r in rows]
                if _push(url, key, remote, conflict, payload):
                    pushed += len(rows)
                else:
                    ok = False
                    break
                offset += len(rows)
                if len(rows) < _BATCH_LIMIT:
                    break
            if not ok:
                errors.append(local)
    return pushed, errors


def _sync_photos(cn, url: str, key: str) -> tuple[int, list[str]]:
    """Upload photo binaries to Storage, then their metadata rows. Idempotent:
    a photo is uploaded once (``synced_at`` flag), never re-sent."""
    bucket = settings.get("supabase_bucket")
    uploaded, errors = 0, []
    root = data_dir().resolve()
    rows = [dict(r) for r in cn.execute(
        "SELECT * FROM photos WHERE synced_at IS NULL AND abs_path IS NOT NULL"
        " ORDER BY created_at LIMIT ?", (_BATCH_LIMIT,))]
    for r in rows:
        p = Path(r["abs_path"]).resolve()
        # Containment: never read (and upload to a public bucket) a file that
        # sits outside the engine data dir. A bundle manifest could otherwise
        # point abs_path at an arbitrary host file — belt-and-braces with the
        # intake-time traversal guard.
        if root != p and root not in p.parents:
            logger.warning("Skipping photo outside data dir: %s", r["abs_path"])
            continue
        if not p.exists():
            continue
        obj = _object_path(r["batch_id"] or "loose", r["file"] or p.name)
        ctype = _CONTENT_TYPES.get(p.suffix.lower(), "application/octet-stream")
        if not _upload_object(url, key, bucket, obj, p.read_bytes(), ctype):
            errors.append(r["photo_id"])
            continue
        public = _public_url(url, bucket, obj)
        meta = {"node": _NODE, "photo_id": r["photo_id"], "batch_id": r["batch_id"],
                "file": r["file"], "sha256": r["sha256"], "captured_at": r["captured_at"],
                "lat": r["lat"], "lng": r["lng"], "width": r["width"],
                "height": r["height"], "bytes": r["bytes"], "blur_score": r["blur_score"],
                "camera_model": r["camera_model"], "remote_url": public}
        if not _push(url, key, _PHOTOS_TABLE, "photo_id", [meta]):
            errors.append(r["photo_id"])
            continue
        cn.execute("UPDATE photos SET synced_at=?, remote_url=? WHERE photo_id=?",
                   (datetime.now(timezone.utc).isoformat(), public, r["photo_id"]))
        cn.commit()
        uploaded += 1
    return uploaded, errors


def sync_now(tables: list[str] | None = None,
             include_photos: bool | None = None) -> dict[str, Any]:
    """Push selected (default: all) summary tables and, by default, photos.

    ``tables=None`` syncs everything including photo binaries. Passing an
    explicit ``tables`` list syncs only those structured tables; photos are
    included only when ``include_photos`` is true (or "photos" is listed).
    """
    status = sync_status()
    if not status["enabled"]:
        return {"mode": "off", "pushed": 0}
    url, key = _env()
    if not (url and key):
        return {"mode": "unconfigured", "pushed": 0}

    do_photos = include_photos if include_photos is not None else \
        (tables is None or "photos" in tables)
    table_filter = [t for t in tables if t != "photos"] if tables is not None else None

    cn = open_conn()
    try:
        pushed, errors = _sync_tables(cn, url, key, table_filter)
        photos, photo_errors = (_sync_photos(cn, url, key) if do_photos else (0, []))
    finally:
        cn.close()
    out: dict[str, Any] = {"mode": "ready", "pushed": pushed, "photos": photos}
    if errors or photo_errors:
        out["errors"] = errors + [f"photo:{e}" for e in photo_errors]
    return out


def sync_async() -> bool:
    """Fire-and-forget sync on a daemon thread. No-op (returns False) unless
    sharing is on, credentials exist, and auto-sync is enabled. A trigger that
    arrives mid-run does not drop its write: it sets a pending flag so the
    running thread does one more pass after it finishes — the last mutation of
    a burst always reaches the cloud."""
    if not settings.get("auto_sync"):
        return False
    if sync_status()["mode"] != "ready":
        return False
    global _sync_running, _sync_pending
    with _sync_lock:
        if _sync_running:
            _sync_pending = True
            return False
        _sync_running = True
        _sync_pending = False

    def _run() -> None:
        global _sync_running, _sync_pending
        while True:
            try:
                sync_now()
            except Exception:  # pragma: no cover - background safety net
                logger.exception("background Supabase sync failed")
            with _sync_lock:
                # Clearing the running flag and observing the pending flag in
                # one locked section: a trigger that arrives after this either
                # sees running=False and starts a fresh thread, or set pending
                # before we got here and gets swept below. No dropped writes,
                # no clobbering of a successor thread.
                if not _sync_pending:
                    _sync_running = False
                    return
                _sync_pending = False  # a trigger landed mid-run — sweep again

    threading.Thread(target=_run, name="stockopoly-sync", daemon=True).start()
    return True


def outbox_path() -> Path:
    return data_dir() / "supabase_sync.jsonl"
