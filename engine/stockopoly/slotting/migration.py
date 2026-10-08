"""Day-by-day migration: turn a slotting plan into a worked task list.

Moves = differences between current inventory placement and the plan's
assignments, ordered by travel-cost benefit so the highest-payoff moves
happen first. Each day holds at most ``daily_move_budget`` tasks; minutes
are estimated from a fixed handling time plus travel distance.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .. import locations as loc_mod, scb_link, settings
from ..store import open_conn

_HANDLING_MIN = 4.0
_MIN_PER_1000_IN = 1.5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_tasks(plan_id: str) -> dict[str, Any]:
    cn = open_conn()
    try:
        budget = int(settings.get("daily_move_budget", cn=cn))
        plan = cn.execute("SELECT * FROM slotting_plans WHERE plan_id=?",
                          (plan_id,)).fetchone()
        if plan is None:
            raise KeyError(f"Unknown plan {plan_id}")
        future = cn.execute(
            "SELECT part_number, location_code, qty_target, role, travel_cost"
            " FROM assignments_future WHERE plan_id=?", (plan_id,)).fetchall()
        current = cn.execute(
            "SELECT part_number, location_code, qty_oh FROM inventory"
            " WHERE qty_oh>0").fetchall()
        cn.execute("DELETE FROM move_tasks WHERE plan_id=?", (plan_id,))
    finally:
        cn.close()

    costs = loc_mod.travel_costs()
    cur: dict[str, list[list]] = {}
    for r in current:
        cur.setdefault(r["part_number"], []).append(
            [r["location_code"], float(r["qty_oh"])])

    moves: list[dict[str, Any]] = []
    for f in future:
        pn, dest, want = f["part_number"], f["location_code"], float(f["qty_target"])
        have_here = 0.0
        for lot in cur.get(pn, []):
            if lot[0] == dest:
                take = min(lot[1], want)
                have_here += take
                lot[1] -= take
        want -= have_here
        for lot in sorted(cur.get(pn, []), key=lambda l: -l[1]):
            if want <= 1e-9:
                break
            if lot[0] == dest or lot[1] <= 0:
                continue
            qty = min(lot[1], want)
            benefit = (costs.get(lot[0], 0.0) - costs.get(dest, 0.0)) * qty
            dist = abs(costs.get(lot[0], 0.0)) + abs(costs.get(dest, 0.0))
            moves.append({
                "part_number": pn, "from": lot[0], "to": dest, "qty": qty,
                "reason": f"{f['role']} slot per plan",
                "benefit": benefit,
                "est_minutes": round(_HANDLING_MIN
                                     + dist / 1000.0 * _MIN_PER_1000_IN, 1),
            })
            lot[1] -= qty
            want -= qty

    moves.sort(key=lambda m: -m["benefit"])
    total_days = math.ceil(len(moves) / budget) if moves else 0
    cn = open_conn()
    try:
        for i, m in enumerate(moves):
            day = i // budget + 1
            cn.execute(
                "INSERT INTO move_tasks(plan_id, day, seq, part_number,"
                " from_location, to_location, qty, reason, est_minutes, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,'todo')",
                (plan_id, day, i % budget + 1, m["part_number"], m["from"],
                 m["to"], round(m["qty"], 2), m["reason"], m["est_minutes"]))
        cn.execute("UPDATE slotting_plans SET total_moves=?, total_days=?"
                   " WHERE plan_id=?", (len(moves), total_days, plan_id))
        cn.commit()
    finally:
        cn.close()

    # Emit actionable body directive to SCB for physical warehouse execution
    if moves:
        top_move = moves[0]
        scb_link.emit_body_directive(
            title=f"Slotting Migration {plan_id}: {len(moves)} moves across {total_days} days",
            why_it_matters=f"Optimizes warehouse travel cost and picker fatigue; top move relocates {top_move['part_number']} from {top_move['from']} to {top_move['to']} for {top_move['benefit']:.1f} units benefit.",
            do_this=f"Pickers/AGVs execute Day 1 move tasks in StockOpoly (budget={budget} moves/day).",
            owner_role="Ops",
            priority=0.85,
            severity="act",
            fingerprint=f"stockopoly:slotting_plan:{plan_id}",
            evidence={"plan_id": plan_id, "moves_count": len(moves), "days": total_days, "daily_budget": budget},
        )

    return {"plan_id": plan_id, "moves": len(moves), "days": total_days,
            "daily_budget": budget}


def list_tasks(plan_id: str, day: int | None = None) -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        if day is None:
            rows = cn.execute("SELECT * FROM move_tasks WHERE plan_id=?"
                              " ORDER BY day, seq", (plan_id,)).fetchall()
        else:
            rows = cn.execute("SELECT * FROM move_tasks WHERE plan_id=? AND day=?"
                              " ORDER BY seq", (plan_id, day)).fetchall()
        return [dict(r) for r in rows]
    finally:
        cn.close()


def complete_task(task_id: int) -> dict[str, Any]:
    """Mark done and apply the stock move to the inventory table."""
    cn = open_conn()
    try:
        t = cn.execute("SELECT * FROM move_tasks WHERE task_id=?",
                       (task_id,)).fetchone()
        if t is None:
            raise KeyError(f"Unknown task {task_id}")
        if t["status"] == "done":
            return dict(t)
        qty = float(t["qty"])
        cn.execute(
            "UPDATE inventory SET qty_oh = MAX(qty_oh - ?, 0)"
            " WHERE part_number=? AND location_code=?",
            (qty, t["part_number"], t["from_location"]))
        cn.execute(
            "INSERT INTO inventory(part_number, location_code, qty_oh, as_of)"
            " VALUES (?,?,?,?) ON CONFLICT(part_number, location_code)"
            " DO UPDATE SET qty_oh = inventory.qty_oh + ?, as_of=excluded.as_of",
            (t["part_number"], t["to_location"], qty,
             _now()[:10], qty))
        cn.execute("UPDATE move_tasks SET status='done', done_at=?"
                   " WHERE task_id=?", (_now(), task_id))
        cn.commit()
        return dict(cn.execute("SELECT * FROM move_tasks WHERE task_id=?",
                               (task_id,)).fetchone())
    finally:
        cn.close()
