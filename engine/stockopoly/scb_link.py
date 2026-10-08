"""Always-on learning bridge into the Supply-Chain-Brain.

Every significant StockOpoly event is written into something the Brain learns
from, independent of the Supabase sharing toggle:

1. Preferred: append a row to the Brain's ``learning_log`` table in
   ``local_brain.sqlite`` (sibling checkout discovery; ``SCB_REPO_DIR`` /
   ``SCB_DB_PATH`` env overrides). This is the established external-writer
   pattern — the Brain's photogrammetry intake creates/append the same table.
2. Fallback (Brain absent or DB locked): queue in the local ``scb_outbox``
   table AND mirror to ``engine/data/scb_learning_outbox.jsonl`` in the same
   row shape as the Brain's ``cloud_learning_queue.jsonl`` so resumption
   tooling can ingest it later. ``flush_outbox()`` retries delivery.

Stdlib only; failures are never raised to callers — learning must not break
the operation that generated the event.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .store import data_dir, open_conn as _open_own_conn

logger = logging.getLogger(__name__)

_ENGINE_DIR = Path(__file__).resolve().parent.parent  # engine/


def _outbox_jsonl() -> Path:
    return data_dir() / "scb_learning_outbox.jsonl"

_LEARNING_DDL = (
    "CREATE TABLE IF NOT EXISTS learning_log("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "logged_at TEXT, kind TEXT, title TEXT, detail TEXT,"
    "signal_strength REAL)"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_scb_repo() -> Path | None:
    env = os.environ.get("SCB_REPO_DIR", "")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    repo_root = _ENGINE_DIR.parent  # StockOpoly/
    for name in (
        "VS Code",
        "VS-Code",
        "Supply-Chain-Brain",
        "supply-chain-brain",
        "Supply-Chain-Brain-main",
    ):
        cand = repo_root.parent / name
        if (cand / "pipeline").is_dir():
            return cand
    return None


def scb_db_path() -> Path | None:
    env = os.environ.get("SCB_DB_PATH", "")
    if env:
        return Path(env).expanduser().resolve()
    repo = find_scb_repo()
    if repo is None:
        return None
    return repo / "pipeline" / "local_brain.sqlite"


def _write_direct(logged_at: str, kind: str, title: str, detail: str, signal: float) -> bool:
    db = scb_db_path()
    if db is None or not db.parent.is_dir():
        return False
    try:
        cn = sqlite3.connect(str(db), timeout=5)
        try:
            cn.execute("PRAGMA journal_mode=WAL")
            cn.execute("PRAGMA busy_timeout=5000")
            cn.execute(_LEARNING_DDL)
            cn.execute(
                "INSERT INTO learning_log(logged_at, kind, title, detail, signal_strength) "
                "VALUES(?,?,?,?,?)",
                (logged_at, kind, title, detail, signal),
            )
            cn.commit()
            return True
        finally:
            cn.close()
    except sqlite3.Error as exc:
        logger.debug("SCB learning_log write failed: %s", exc)
        return False


def _queue_outbox(logged_at: str, kind: str, title: str, detail: str, signal: float) -> None:
    try:
        cn = _open_own_conn()
        try:
            cn.execute(
                "INSERT INTO scb_outbox(logged_at, kind, title, detail, signal_strength) "
                "VALUES(?,?,?,?,?)",
                (logged_at, kind, title, detail, signal),
            )
            cn.commit()
        finally:
            cn.close()
    except sqlite3.Error as exc:
        logger.warning("scb_outbox insert failed: %s", exc)
    try:
        jsonl = _outbox_jsonl()
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        row = {"logged_at": logged_at, "kind": kind, "title": title,
               "detail": detail, "signal_strength": signal, "node": "stockopoly"}
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError as exc:
        logger.warning("outbox JSONL append failed: %s", exc)


def log_learning(kind: str, title: str, detail: dict | str, signal: float = 0.5) -> bool:
    """Record a learning event. Returns True when delivered to SCB directly."""
    logged_at = _now()
    detail_str = detail if isinstance(detail, str) else json.dumps(detail, separators=(",", ":"))
    if _write_direct(logged_at, kind, title, detail_str, signal):
        return True
    _queue_outbox(logged_at, kind, title, detail_str, signal)
    return False


def emit_body_directive(
    title: str,
    why_it_matters: str,
    do_this: str,
    owner_role: str = "Ops",
    priority: float = 0.8,
    severity: str = "act",
    fingerprint: str | None = None,
    evidence: dict | None = None,
    value_per_year: float | None = None,
) -> bool:
    """Emit an actionable physical directive into SCB's body_directives table."""
    db = scb_db_path()
    if db is None or not db.parent.is_dir():
        return False
    fp = fingerprint or f"stockopoly:{title}:{_now()}"
    created_at = _now()
    evidence_json = json.dumps(evidence or {}, default=str)
    try:
        cn = sqlite3.connect(str(db), timeout=5)
        try:
            cn.execute("PRAGMA busy_timeout=5000")
            cn.execute(
                """
                INSERT INTO body_directives (
                    created_at, fingerprint, source, signal_kind, priority,
                    severity, title, why_it_matters, do_this, owner_role,
                    target_entity, evidence_json, status, last_status_at, value_per_year
                ) VALUES (?, ?, 'stockopoly', 'warehouse_rebalance', ?, ?, ?, ?, ?, ?, 'warehouse::slotting', ?, 'open', ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    priority=excluded.priority,
                    severity=excluded.severity,
                    last_status_at=excluded.last_status_at
                """,
                (
                    created_at,
                    fp,
                    priority,
                    severity,
                    title,
                    why_it_matters,
                    do_this,
                    owner_role,
                    evidence_json,
                    created_at,
                    value_per_year,
                ),
            )
            cn.commit()
            return True
        finally:
            cn.close()
    except sqlite3.Error as exc:
        logger.debug("SCB body_directives write failed: %s", exc)
        return False



def flush_outbox(limit: int = 500) -> int:
    """Retry delivery of queued events into SCB's learning_log."""
    cn = _open_own_conn()
    try:
        rows = cn.execute(
            "SELECT id, logged_at, kind, title, detail, signal_strength "
            "FROM scb_outbox WHERE flushed=0 ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        flushed = 0
        for r in rows:
            if _write_direct(r["logged_at"], r["kind"], r["title"], r["detail"],
                             r["signal_strength"] or 0.5):
                cn.execute(
                    "UPDATE scb_outbox SET flushed=1, flushed_at=? WHERE id=?",
                    (_now(), r["id"]),
                )
                flushed += 1
            else:
                break  # SCB still unreachable; keep order, stop early
        cn.commit()
        return flushed
    finally:
        cn.close()


def status() -> dict:
    repo = find_scb_repo()
    db = scb_db_path()
    pending = 0
    try:
        cn = _open_own_conn()
        try:
            pending = cn.execute(
                "SELECT COUNT(*) FROM scb_outbox WHERE flushed=0"
            ).fetchone()[0]
        finally:
            cn.close()
    except sqlite3.Error:
        pass
    return {
        "scb_repo": str(repo) if repo else None,
        "scb_db": str(db) if db else None,
        "scb_db_exists": bool(db and db.exists()),
        "outbox_pending": pending,
        "outbox_jsonl": str(_outbox_jsonl()),
    }
