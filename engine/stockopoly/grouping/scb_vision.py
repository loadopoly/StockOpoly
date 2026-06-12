"""Tier-1 grouping — SCB-native knowledge (opt-in via the scb_vision setting).

Asks the Supply-Chain-Brain's local store what it already knows about these
photos. If a photo's SHA-256 appears in the Brain's ``photogrammetry_photos``
table, all batch photos sharing that SCB session are — by the Brain's own
record — one capture subject. Read-only, stdlib sqlite3, never raises.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ..scb_link import scb_db_path

logger = logging.getLogger(__name__)


def propose(photos: list[dict], batch: dict) -> list[dict[str, Any]]:
    db = scb_db_path()
    if db is None or not db.exists():
        return []
    by_session: dict[str, list[str]] = {}
    session_names: dict[str, str] = {}
    try:
        cn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            cn.row_factory = sqlite3.Row
            have = {r["name"] for r in cn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "photogrammetry_photos" not in have:
                return []
            for p in photos:
                sha = p.get("sha256")
                if not sha:
                    continue
                row = cn.execute(
                    "SELECT session_id FROM photogrammetry_photos WHERE sha256=? LIMIT 1",
                    (sha,)).fetchone()
                if row:
                    by_session.setdefault(row["session_id"], []).append(p["photo_id"])
            if by_session and "photogrammetry_sessions" in have:
                for sid in by_session:
                    row = cn.execute(
                        "SELECT session_name FROM photogrammetry_sessions"
                        " WHERE session_id=?", (sid,)).fetchone()
                    if row and row["session_name"]:
                        session_names[sid] = row["session_name"]
        finally:
            cn.close()
    except sqlite3.Error as exc:
        logger.debug("SCB vision lookup failed: %s", exc)
        return []

    return [
        {
            "kind": "same_object",
            "label": session_names.get(sid, f"SCB session {sid[:8]}"),
            "confidence": 0.85,
            "photo_ids": sorted(members),
            "meta": {"signal": "scb_session", "scb_session_id": sid},
        }
        for sid, members in by_session.items()
        if len(members) >= 2
    ]
