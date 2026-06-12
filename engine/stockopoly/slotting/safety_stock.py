"""Safety stock under three service-level scenarios.

For each part: monthly demand mean/σ from the trailing 12 usage buckets,
lead time from PO history (mean receipt−order days, falling back to the
``default_lead_time_days`` setting), then the classic formula

    SS  = z · σ_month · √(LT_months)
    min = SS + mean_month · LT_months          (reorder point)
    max = min + mean_month                      (one review period of cover)

Scenarios: conservative z = 1.88 (≈ 97 %), baseline z = 1.65 (≈ 95 %),
aggressive z = 1.28 (≈ 90 %). Per-part ``overrides_json`` may pin
``lead_time_days`` or ``z``.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from .. import settings
from ..store import open_conn
from .velocity import _trailing_periods

SCENARIO_Z = {"conservative": 1.88, "baseline": 1.65, "aggressive": 1.28}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lead_times(cn, default_days: float) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in cn.execute(
            "SELECT part_number,"
            " AVG(JULIANDAY(receipt_date) - JULIANDAY(order_date)) AS lt"
            " FROM po_history WHERE order_date IS NOT NULL"
            " AND receipt_date IS NOT NULL GROUP BY part_number"):
        if r["lt"] and r["lt"] > 0:
            out[r["part_number"]] = float(r["lt"])
    return out


def compute_safety_stock(scenario: str | None = None) -> dict[str, Any]:
    cn = open_conn()
    try:
        scen = scenario or str(settings.get("ss_scenario", cn=cn))
        if scen not in SCENARIO_Z:
            raise ValueError(f"Scenario must be one of {sorted(SCENARIO_Z)}")
        default_lt = float(settings.get("default_lead_time_days", cn=cn))
        latest = cn.execute("SELECT MAX(period) AS p FROM usage_history"
                            ).fetchone()["p"]
        periods = _trailing_periods(12, latest)
        qmarks = ",".join("?" * len(periods))
        monthly: dict[str, dict[str, float]] = {}
        for r in cn.execute(
                f"SELECT part_number, period, SUM(qty) AS qty FROM usage_history"
                f" WHERE period IN ({qmarks}) AND kind='issue'"
                f" GROUP BY part_number, period", periods):
            monthly.setdefault(r["part_number"], {})[r["period"]] = float(r["qty"])
        lead = _lead_times(cn, default_lt)
        overrides: dict[str, dict] = {}
        for r in cn.execute("SELECT part_number, overrides_json FROM ss_params"
                            " WHERE overrides_json IS NOT NULL"):
            try:
                overrides[r["part_number"]] = json.loads(r["overrides_json"])
            except json.JSONDecodeError:
                pass

        now = _now()
        computed = 0
        for pn, buckets_map in monthly.items():
            buckets = [buckets_map.get(p, 0.0) for p in periods]
            mean = sum(buckets) / 12.0
            std = math.sqrt(sum((b - mean) ** 2 for b in buckets) / 12.0)
            ov = overrides.get(pn, {})
            lt_days = float(ov.get("lead_time_days", lead.get(pn, default_lt)))
            z = float(ov.get("z", SCENARIO_Z[scen]))
            lt_m = max(lt_days / 30.0, 1e-6)
            ss = z * std * math.sqrt(lt_m)
            min_qty = ss + mean * lt_m
            max_qty = min_qty + mean
            cn.execute(
                "INSERT INTO ss_params(part_number, demand_mean_m, demand_std_m,"
                " lead_time_days, scenario, overrides_json, ss_qty, min_qty,"
                " max_qty, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(part_number) DO UPDATE SET"
                " demand_mean_m=excluded.demand_mean_m,"
                " demand_std_m=excluded.demand_std_m,"
                " lead_time_days=excluded.lead_time_days,"
                " scenario=excluded.scenario,"
                " ss_qty=excluded.ss_qty, min_qty=excluded.min_qty,"
                " max_qty=excluded.max_qty, computed_at=excluded.computed_at",
                (pn, round(mean, 3), round(std, 3), round(lt_days, 1), scen,
                 json.dumps(ov, separators=(",", ":")) if ov else None,
                 round(ss, 2), round(min_qty, 2), round(max_qty, 2), now))
            computed += 1
        cn.commit()
        return {"scenario": scen, "z": SCENARIO_Z[scen], "parts": computed}
    finally:
        cn.close()


def scenario_compare(part_number: str) -> dict[str, Any]:
    """The three scenarios side by side for one part (UI what-if view)."""
    cn = open_conn()
    try:
        row = cn.execute("SELECT * FROM ss_params WHERE part_number=?",
                         (part_number,)).fetchone()
        if row is None:
            raise KeyError(f"No safety-stock params for {part_number}")
        mean, std = float(row["demand_mean_m"] or 0), float(row["demand_std_m"] or 0)
        lt_m = max(float(row["lead_time_days"] or 14) / 30.0, 1e-6)
        out = {"part_number": part_number, "demand_mean_m": mean,
               "demand_std_m": std, "lead_time_days": row["lead_time_days"],
               "scenarios": {}}
        for name, z in SCENARIO_Z.items():
            ss = z * std * math.sqrt(lt_m)
            out["scenarios"][name] = {
                "z": z, "ss_qty": round(ss, 2),
                "min_qty": round(ss + mean * lt_m, 2),
                "max_qty": round(ss + mean * lt_m + mean, 2)}
        return out
    finally:
        cn.close()


def list_ss() -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        return [dict(r) for r in cn.execute(
            "SELECT * FROM ss_params ORDER BY part_number")]
    finally:
        cn.close()
