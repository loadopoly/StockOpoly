"""Photo-grouping cascade + manual group CRUD.

Cascade order (cheapest trustworthy signal wins; later tiers only add):

* tier 1 — SCB-native session knowledge (``scb_vision`` setting, default off)
* tier 2 — offline heuristics (always runs)
* tier 3 — vision LLM (``llm_vision`` setting, default off): routed through the
  Supply-Chain-Brain ensemble when reachable (``scb_dispatch`` — the Brain owns
  model selection + the multi-provider caller), else a direct OpenRouter/Grok
  call (``llm``, needs ``OPENROUTER_API_KEY``). ``llm_vision_prefer_scb`` (default
  on) controls the preference.

``run_cascade`` replaces previous *auto* proposals (source_tier ≥ 1,
unconfirmed) but never touches manual groups (tier 0) or anything a human
confirmed. Group kinds: same_object · scale_reference · relational_size ·
location_label (plus free-form custom kinds from the UI).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import events, settings
from ..store import open_conn
from . import heuristics, llm, scb_dispatch, scb_vision

__all__ = [
    "run_cascade", "create_group", "confirm_group", "delete_group",
    "set_members", "list_groups", "group_detail",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _batch_row(cn, batch_id: str) -> dict:
    row = cn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown batch: {batch_id}")
    return dict(row)


def _batch_photos(cn, batch_id: str) -> list[dict]:
    rows = cn.execute(
        "SELECT photo_id, file, abs_path, sha256, captured_at, lat, lng, dhash"
        " FROM photos WHERE batch_id=? ORDER BY file", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def _insert_group(cn, batch_id: str, proposal: dict, tier: int) -> str:
    group_id = f"grp_{uuid.uuid4().hex[:12]}"
    cn.execute(
        "INSERT INTO photo_groups(group_id, batch_id, kind, label, confidence,"
        " source_tier, meta_json, confirmed, created_at) VALUES (?,?,?,?,?,?,?,0,?)",
        (group_id, batch_id, proposal["kind"], proposal["label"],
         proposal["confidence"], tier,
         json.dumps(proposal.get("meta") or {}, separators=(",", ":")), _now()),
    )
    for pid in proposal["photo_ids"]:
        cn.execute(
            "INSERT OR REPLACE INTO photo_group_members(group_id, photo_id, role,"
            " confidence) VALUES (?,?,?,?)",
            (group_id, pid, proposal.get("role", "member"), proposal["confidence"]),
        )
    return group_id


def _covered(existing: list[dict], proposal: dict) -> bool:
    """True when an existing group already covers this proposal's members."""
    members = set(proposal["photo_ids"])
    for g in existing:
        if g["kind"] == proposal["kind"] and members <= set(g["photo_ids"]):
            return True
    return False


def run_cascade(batch_id: str) -> dict[str, Any]:
    """(Re)propose groups for a batch. Returns {tiers_run, created, groups}."""
    cfg = settings.all_settings()
    cn = open_conn()
    try:
        batch = _batch_row(cn, batch_id)
        photos = _batch_photos(cn, batch_id)

        # Drop stale auto-proposals; keep manual (tier 0) + confirmed groups.
        stale = [r["group_id"] for r in cn.execute(
            "SELECT group_id FROM photo_groups"
            " WHERE batch_id=? AND source_tier>=1 AND confirmed=0", (batch_id,))]
        for gid in stale:
            cn.execute("DELETE FROM photo_group_members WHERE group_id=?", (gid,))
            cn.execute("DELETE FROM photo_groups WHERE group_id=?", (gid,))

        kept = list_groups(batch_id, cn=cn)
        tiers_run: list[int] = []
        created = 0
        floor = float(cfg.get("group_confidence_floor", 0.55))

        def apply(proposals: list[dict], tier: int) -> None:
            nonlocal created
            for pr in proposals:
                if pr["confidence"] < floor and pr["kind"] != "scale_reference":
                    continue
                if _covered(kept, pr):
                    continue
                gid = _insert_group(cn, batch_id, pr, tier)
                kept.append({"group_id": gid, "kind": pr["kind"],
                             "photo_ids": pr["photo_ids"]})
                created += 1

        if cfg.get("scb_vision"):
            tiers_run.append(1)
            apply(scb_vision.propose(photos, batch), 1)

        tiers_run.append(2)
        apply(heuristics.propose(photos, batch, cfg), 2)

        if cfg.get("llm_vision"):
            tiers_run.append(3)
            # Prefer the Brain's ensemble (model registry + router + multi-provider
            # caller); fall back to the standalone OpenRouter call when the Brain
            # is unreachable. ``None`` means "Brain didn't run" → fall back; an
            # empty list means "Brain ran, nothing to add" → don't double-spend.
            proposals: list[dict] | None = None
            if cfg.get("llm_vision_prefer_scb", True):
                proposals = scb_dispatch.propose(photos, batch, cfg)
            if proposals is None:
                proposals = llm.propose(photos, batch, cfg)
            apply(proposals, 3)

        cn.commit()
        groups = list_groups(batch_id, cn=cn)
    finally:
        cn.close()

    summary = {"batch_id": batch_id, "tiers_run": tiers_run,
               "created": created, "group_count": len(groups)}
    events.record("stockopoly_grouping", summary,
                  title=f"Grouping [{batch_id[:8]}]: {created} proposal(s)",
                  signal=min(0.3 + 0.1 * created, 0.9))
    return {**summary, "groups": groups}


# ───────────────────────────────────────────────────────────────── manual CRUD
def create_group(batch_id: str, kind: str, label: str, photo_ids: list[str],
                 *, confidence: float = 1.0, meta: dict | None = None) -> str:
    cn = open_conn()
    try:
        _batch_row(cn, batch_id)
        gid = _insert_group(cn, batch_id, {
            "kind": kind, "label": label, "confidence": confidence,
            "photo_ids": photo_ids, "meta": meta or {"signal": "manual"},
        }, tier=0)
        cn.execute("UPDATE photo_groups SET confirmed=1 WHERE group_id=?", (gid,))
        cn.commit()
        return gid
    finally:
        cn.close()


def confirm_group(group_id: str, confirmed: bool = True) -> None:
    cn = open_conn()
    try:
        cn.execute("UPDATE photo_groups SET confirmed=? WHERE group_id=?",
                   (1 if confirmed else 0, group_id))
        cn.commit()
    finally:
        cn.close()


def delete_group(group_id: str) -> None:
    cn = open_conn()
    try:
        cn.execute("DELETE FROM photo_group_members WHERE group_id=?", (group_id,))
        cn.execute("DELETE FROM photo_groups WHERE group_id=?", (group_id,))
        cn.commit()
    finally:
        cn.close()


def set_members(group_id: str, photo_ids: list[str]) -> None:
    cn = open_conn()
    try:
        cn.execute("DELETE FROM photo_group_members WHERE group_id=?", (group_id,))
        for pid in photo_ids:
            cn.execute(
                "INSERT INTO photo_group_members(group_id, photo_id, role, confidence)"
                " VALUES (?,?,'member',1.0)", (group_id, pid))
        cn.commit()
    finally:
        cn.close()


def list_groups(batch_id: str, cn=None) -> list[dict[str, Any]]:
    own = cn is None
    if own:
        cn = open_conn()
    try:
        rows = cn.execute(
            "SELECT group_id, kind, label, confidence, source_tier, confirmed,"
            " meta_json, created_at FROM photo_groups WHERE batch_id=?"
            " ORDER BY created_at, group_id", (batch_id,)).fetchall()
        out = []
        for r in rows:
            members = [m["photo_id"] for m in cn.execute(
                "SELECT photo_id FROM photo_group_members WHERE group_id=?"
                " ORDER BY photo_id", (r["group_id"],))]
            try:
                meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
            except json.JSONDecodeError:
                meta = {}
            out.append({"group_id": r["group_id"], "kind": r["kind"],
                        "label": r["label"], "confidence": r["confidence"],
                        "source_tier": r["source_tier"],
                        "confirmed": bool(r["confirmed"]), "meta": meta,
                        "photo_ids": members})
        return out
    finally:
        if own:
            cn.close()


def group_detail(group_id: str) -> dict[str, Any]:
    cn = open_conn()
    try:
        row = cn.execute("SELECT * FROM photo_groups WHERE group_id=?",
                         (group_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown group: {group_id}")
        members = cn.execute(
            "SELECT m.photo_id, m.role, p.file, p.abs_path, p.captured_at"
            " FROM photo_group_members m JOIN photos p ON p.photo_id=m.photo_id"
            " WHERE m.group_id=? ORDER BY p.file", (group_id,)).fetchall()
        return {**dict(row), "members": [dict(m) for m in members]}
    finally:
        cn.close()
