"""Velocity → occupancy → optimizer → migration → safety stock pipeline."""
from __future__ import annotations

import pytest

from stockopoly import imports, locations, settings, slotting
from stockopoly.store import open_conn


def _q(sql, *args):
    cn = open_conn()
    try:
        return cn.execute(sql, args).fetchall()
    finally:
        cn.close()


@pytest.fixture()
def warehouse():
    """Three parts: FAST (steady mover stored far away), MED, SLOW (parked
    in the best slot). Locations across two levels and bays 1/5."""
    imports.import_parts([
        {"part": "FAST", "cost": 10.0, "length": 12, "width": 12, "height": 12},
        {"part": "MED", "cost": 5.0, "length": 10, "width": 10, "height": 10},
        {"part": "SLOW", "cost": 1.0, "length": 8, "width": 8, "height": 8},
    ])
    usage = []
    for i in range(1, 13):
        period = f"2026-{i:02d}" if i <= 6 else f"2025-{i:02d}"
        usage.append({"part": "FAST", "period": period, "qty": 100})
        if i % 2 == 0:
            usage.append({"part": "MED", "period": period, "qty": 50})
    usage.append({"part": "SLOW", "period": "2026-01", "qty": 2})
    imports.import_usage(usage)
    imports.import_inventory([
        {"part": "FAST", "location": "A-05-1", "qty": 40},
        {"part": "MED", "location": "A-05-2", "qty": 30},
        {"part": "SLOW", "location": "A-01-1", "qty": 10},
    ])
    locations.register_locations(
        ["A-01-1", "A-01-2", "A-02-1", "A-02-2", "A-05-1", "A-05-2"])
    locations.compute_coordinates()
    return None


def test_velocity_classes(warehouse):
    out = slotting.compute_velocity()
    assert out["parts"] == 3
    vel = {v["part_number"]: v for v in slotting.list_velocity()}
    assert vel["FAST"]["abc_class"] == "A" and vel["FAST"]["xyz_class"] == "X"
    assert vel["MED"]["xyz_class"] == "Y"
    assert vel["SLOW"]["abc_class"] == "C" and vel["SLOW"]["xyz_class"] == "Z"
    assert vel["FAST"]["velocity_score"] > vel["MED"]["velocity_score"] \
        > vel["SLOW"]["velocity_score"]
    assert vel["FAST"]["usage_12m_qty"] == 1200
    assert vel["FAST"]["months_of_supply"] == pytest.approx(0.4, abs=0.01)


def test_occupancy_states(warehouse):
    imports.import_parts([{"part": "BIG", "length": 24, "width": 24, "height": 24}])
    imports.import_inventory([{"part": "BIG", "location": "A-02-1", "qty": 200}])
    out = slotting.compute_occupancy()
    assert out["locations"] == 6
    assert out["over"] == 1 and out["empty"] >= 1
    occ = {o["location_code"]: o for o in slotting.current_state()}
    assert occ["A-02-1"]["status"] == "over"
    assert occ["A-05-1"]["status"] == "ok"
    assert occ["A-02-1"]["inventory"][0]["part_number"] == "BIG"


def test_optimizer_moves_fast_close_and_respects_golden(warehouse):
    settings.put("golden_zone_levels", [1])
    slotting.compute_velocity()
    summary = slotting.optimize()
    assert summary["parts_assigned"] == 3
    assert summary["objective_after"] < summary["objective_before"]
    assert summary["improvement_pct"] > 0

    rows = slotting.plan_assignments(summary["plan_id"])
    prime = {r["part_number"]: r for r in rows if r["role"] == "prime"}
    # FAST claims the cheapest slot (bay 1, level 1 = golden)
    assert prime["FAST"]["location_code"] == "A-01-1"
    # SLOW is not top-quartile → golden level 1 denied while level 2 has room
    slow_level = _q("SELECT level FROM locations WHERE location_code=?",
                    prime["SLOW"]["location_code"])[0]["level"]
    assert slow_level == 2
    evts = [r["kind"] for r in _q("SELECT kind FROM events")]
    assert "stockopoly_slotting_plan" in evts


def test_migration_tasks_and_completion(warehouse):
    settings.put("daily_move_budget", 1)
    slotting.compute_velocity()
    plan = slotting.optimize()
    out = slotting.build_tasks(plan["plan_id"])
    assert out["moves"] >= 2
    assert out["days"] == out["moves"]  # budget 1/day

    tasks = slotting.list_tasks(plan["plan_id"])
    assert [t["day"] for t in tasks] == list(range(1, len(tasks) + 1))
    # first task is the biggest travel win: FAST out of bay 5
    assert tasks[0]["part_number"] == "FAST"
    assert tasks[0]["from_location"] == "A-05-1"

    done = slotting.complete_task(tasks[0]["task_id"])
    assert done["status"] == "done"
    inv = {r["location_code"]: r["qty_oh"] for r in _q(
        "SELECT location_code, qty_oh FROM inventory WHERE part_number='FAST'")}
    assert inv["A-05-1"] == 0.0
    assert inv[tasks[0]["to_location"]] == 40.0


def test_safety_stock_scenarios(warehouse):
    imports.import_po_history([
        {"po": "P1", "part": "MED", "qty": 100, "order date": "2026-01-01",
         "receipt date": "2026-01-31"},
    ])
    out = slotting.compute_safety_stock()
    assert out["scenario"] == "baseline" and out["parts"] == 3

    ss = {r["part_number"]: r for r in slotting.list_ss()}
    # FAST: perfectly steady demand → zero σ → SS 0, min = one LT of demand
    assert ss["FAST"]["ss_qty"] == 0.0
    assert ss["FAST"]["min_qty"] == pytest.approx(
        100 * ss["FAST"]["lead_time_days"] / 30.0, rel=1e-3)
    # MED uses its PO-derived 30-day lead time
    assert ss["MED"]["lead_time_days"] == pytest.approx(30.0, abs=0.1)
    assert ss["MED"]["ss_qty"] > 0

    cmp = slotting.scenario_compare("MED")
    s = cmp["scenarios"]
    assert s["conservative"]["ss_qty"] > s["baseline"]["ss_qty"] \
        > s["aggressive"]["ss_qty"]
    assert s["baseline"]["max_qty"] == pytest.approx(
        s["baseline"]["min_qty"] + cmp["demand_mean_m"], rel=1e-6)
