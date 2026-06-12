"""Future-state slotting optimizer: greedy seed + pairwise-swap refinement.

Objective (lower is better)::

    Σ over parts  picks_per_month(part) × travel_cost(assigned location)

Greedy pass: parts ranked by ``velocity_score`` pick the cheapest location
that (a) has enough usable volume for their on-hand stock and (b) respects
the golden zone — levels listed in ``golden_zone_levels`` are reserved for
the top velocity quartile while supply lasts. Overflow that doesn't fit a
single prime location spills into the cheapest remaining space as
``reserve``. The swap pass then trades prime locations between part pairs
whenever that lowers the objective.

Writes ``slotting_plans`` + ``assignments_future`` and returns the plan
summary; migration tasks are built separately (slotting.migration).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .. import events, locations as loc_mod, settings
from ..store import open_conn
from .occupancy import _unit_volumes

_SWAP_TOP_N = 60          # only the busiest parts join the swap pass
_SWAP_MAX_ROUNDS = 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def optimize(params: dict | None = None) -> dict[str, Any]:
    params = dict(params or {})
    cn = open_conn()
    try:
        cfg = settings.all_settings(cn=cn)
        golden_levels = set(int(x) for x in cfg["golden_zone_levels"])
        unit_v = _unit_volumes(cn, float(cfg["default_unit_volume_in3"]))

        vel = {r["part_number"]: dict(r) for r in cn.execute(
            "SELECT part_number, velocity_score, monthly_hits, usage_12m_qty"
            " FROM velocity")}
        onhand = {r["part_number"]: float(r["qty"]) for r in cn.execute(
            "SELECT part_number, SUM(qty_oh) AS qty FROM inventory"
            " WHERE qty_oh>0 GROUP BY part_number")}
        current_loc = {}
        for r in cn.execute("SELECT part_number, location_code, qty_oh"
                            " FROM inventory WHERE qty_oh>0"):
            cur = current_loc.setdefault(r["part_number"], [])
            cur.append((r["location_code"], float(r["qty_oh"])))
        locs = {r["location_code"]: dict(r) for r in cn.execute(
            "SELECT location_code, level, capacity_volume, fill_factor, status"
            " FROM locations WHERE status='active' AND capacity_volume > 0")}
    finally:
        cn.close()

    costs = loc_mod.travel_costs()
    parts = sorted(onhand, key=lambda p: -(vel.get(p, {}).get("velocity_score") or 0.0))
    if not parts or not locs:
        raise ValueError("Optimizer needs inventory, located locations and travel costs"
                         " (run imports + compute_coordinates first)")

    def picks(pn: str) -> float:
        v = vel.get(pn, {})
        return max(float(v.get("monthly_hits") or 0.0),
                   float(v.get("usage_12m_qty") or 0.0) / 12.0, 0.1)

    top_quartile = set(parts[:max(len(parts) // 4, 1)])
    free = {code: float(l["capacity_volume"]) * float(l["fill_factor"] or 0.85)
            for code, l in locs.items() if code in costs}
    order = sorted(free, key=lambda c: costs[c])

    assignments: dict[str, list[tuple[str, float, str]]] = {}
    prime_of: dict[str, str] = {}
    for pn in parts:
        need = onhand[pn] * unit_v.get(pn, float(64.0))
        qty_left = onhand[pn]
        placed: list[tuple[str, float, str]] = []
        for code in order:
            if qty_left <= 0:
                break
            if free[code] <= 0:
                continue
            level = int(locs[code]["level"] or 1)
            if level in golden_levels and pn not in top_quartile:
                # golden shelf: hold for fast movers unless nothing else fits
                if any(free[c] > 0 and int(locs[c]["level"] or 1) not in golden_levels
                       for c in order):
                    continue
            uv = unit_v.get(pn, 64.0)
            fit_qty = min(qty_left, free[code] / uv) if uv > 0 else qty_left
            if fit_qty < min(qty_left, 1.0) and not placed:
                continue  # prime slot should hold something meaningful
            role = "prime" if not placed else "reserve"
            placed.append((code, fit_qty, role))
            if role == "prime":
                prime_of[pn] = code
            free[code] -= fit_qty * uv
            qty_left -= fit_qty
            need -= fit_qty * uv
        if placed:
            assignments[pn] = placed

    def objective(prime_map: dict[str, str]) -> float:
        return sum(picks(pn) * costs.get(code, 1e6)
                   for pn, code in prime_map.items())

    # Pairwise swap refinement on prime slots of the busiest parts.
    busy = [pn for pn in parts if pn in prime_of][:_SWAP_TOP_N]
    swaps = 0
    for _ in range(_SWAP_MAX_ROUNDS):
        improved = False
        for i in range(len(busy)):
            for j in range(i + 1, len(busy)):
                a, b = busy[i], busy[j]
                ca, cb = prime_of[a], prime_of[b]
                if ca == cb:
                    continue
                delta = ((picks(a) * costs[cb] + picks(b) * costs[ca])
                         - (picks(a) * costs[ca] + picks(b) * costs[cb]))
                if delta < -1e-9:
                    # volumes must still fit at the swapped destinations
                    va = onhand[a] * unit_v.get(a, 64.0)
                    vb = onhand[b] * unit_v.get(b, 64.0)
                    cap_a = float(locs[cb]["capacity_volume"]) * float(locs[cb]["fill_factor"] or 0.85)
                    cap_b = float(locs[ca]["capacity_volume"]) * float(locs[ca]["fill_factor"] or 0.85)
                    if va <= cap_a and vb <= cap_b:
                        prime_of[a], prime_of[b] = cb, ca
                        swaps += 1
                        improved = True
        if not improved:
            break
    for pn, code in prime_of.items():
        rest = [x for x in assignments[pn] if x[2] != "prime"]
        prime_qty = sum(q for _, q, role in assignments[pn] if role == "prime")
        assignments[pn] = [(code, prime_qty, "prime")] + rest

    # Objective before = picks × cost of today's biggest current location.
    cur_prime = {}
    for pn, lots in current_loc.items():
        if pn in prime_of and lots:
            cur_prime[pn] = max(lots, key=lambda lq: lq[1])[0]
    obj_before = sum(picks(pn) * costs.get(code, max(costs.values()) * 1.5)
                     for pn, code in cur_prime.items())
    obj_after = objective(prime_of)

    plan_id = f"plan_{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:4]}"
    cn = open_conn()
    try:
        cn.execute(
            "INSERT INTO slotting_plans(plan_id, created_at, params_json,"
            " objective_before, objective_after, total_moves, total_days)"
            " VALUES (?,?,?,?,?,0,0)",
            (plan_id, _now(),
             json.dumps({**params, "swaps": swaps,
                         "golden_levels": sorted(golden_levels)},
                        separators=(",", ":")),
             round(obj_before, 1), round(obj_after, 1)))
        for pn, placed in assignments.items():
            for rank, (code, qty, role) in enumerate(placed):
                cn.execute(
                    "INSERT OR REPLACE INTO assignments_future(plan_id,"
                    " part_number, location_code, qty_target, role, rank,"
                    " travel_cost) VALUES (?,?,?,?,?,?,?)",
                    (plan_id, pn, code, round(qty, 2), role, rank,
                     costs.get(code)))
        cn.commit()
    finally:
        cn.close()

    summary = {
        "plan_id": plan_id,
        "parts_assigned": len(assignments),
        "objective_before": round(obj_before, 1),
        "objective_after": round(obj_after, 1),
        "improvement_pct": round(100.0 * (1 - obj_after / obj_before), 1)
        if obj_before > 0 else 0.0,
        "swap_count": swaps,
    }
    events.record("stockopoly_slotting_plan", summary,
                  title=f"Slotting plan {plan_id}: "
                        f"{summary['improvement_pct']}% travel reduction",
                  signal=0.6)
    return summary


def plan_assignments(plan_id: str) -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        return [dict(r) for r in cn.execute(
            "SELECT * FROM assignments_future WHERE plan_id=?"
            " ORDER BY part_number, rank", (plan_id,))]
    finally:
        cn.close()


def list_plans() -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        return [dict(r) for r in cn.execute(
            "SELECT * FROM slotting_plans ORDER BY created_at DESC")]
    finally:
        cn.close()
