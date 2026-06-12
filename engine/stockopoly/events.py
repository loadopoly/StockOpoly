"""Local event journal that mirrors significant events into the SCB link."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import scb_link
from .store import open_conn

# Events of these kinds are mirrored into the Brain's learning_log.
_LEARNING_KINDS = {
    "stockopoly_intake", "stockopoly_grouping", "stockopoly_dims_solved",
    "stockopoly_slotting_plan", "stockopoly_crawl", "stockopoly_import",
}


def record(kind: str, payload: dict, *, title: str = "", signal: float = 0.5) -> None:
    at = datetime.now(timezone.utc).isoformat()
    cn = open_conn()
    try:
        cn.execute(
            "INSERT INTO events(at, kind, payload_json) VALUES(?,?,?)",
            (at, kind, json.dumps(payload, separators=(",", ":"))),
        )
        cn.commit()
    finally:
        cn.close()
    if kind in _LEARNING_KINDS:
        scb_link.log_learning(kind, title or kind, payload, signal)


def recent(limit: int = 50) -> list[dict]:
    cn = open_conn()
    try:
        rows = cn.execute(
            "SELECT at, kind, payload_json FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            out.append({"at": r["at"], "kind": r["kind"], "payload": payload})
        return out
    finally:
        cn.close()
