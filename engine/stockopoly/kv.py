"""Key-value helpers over the ``kv`` table (brain_kv conventions)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .store import open_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def kv_set(key: str, value: str, cn: sqlite3.Connection | None = None) -> None:
    own = cn is None
    if own:
        cn = open_conn()
    try:
        cn.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now()),
        )
        cn.commit()
    finally:
        if own:
            cn.close()


def kv_get(key: str, cn: sqlite3.Connection | None = None) -> str | None:
    own = cn is None
    if own:
        cn = open_conn()
    try:
        row = cn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        if own:
            cn.close()


def kv_set_json(key: str, obj: Any, cn: sqlite3.Connection | None = None) -> None:
    kv_set(key, json.dumps(obj, separators=(",", ":")), cn=cn)


def kv_get_json(key: str, default: Any = None, cn: sqlite3.Connection | None = None) -> Any:
    raw = kv_get(key, cn=cn)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def kv_scan_prefix(prefix: str, cn: sqlite3.Connection | None = None) -> list[tuple[str, str]]:
    own = cn is None
    if own:
        cn = open_conn()
    try:
        rows = cn.execute(
            "SELECT key, value FROM kv WHERE key LIKE ? ORDER BY key", (prefix + "%",)
        ).fetchall()
        return [(r["key"], r["value"]) for r in rows]
    finally:
        if own:
            cn.close()


def kv_delete(key: str, cn: sqlite3.Connection | None = None) -> None:
    own = cn is None
    if own:
        cn = open_conn()
    try:
        cn.execute("DELETE FROM kv WHERE key=?", (key,))
        cn.commit()
    finally:
        if own:
            cn.close()
