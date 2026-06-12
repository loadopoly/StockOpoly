"""ABC×XYZ velocity classification from usage history.

ABC slices parts by cumulative 12-month *value* share (A ≤ 80 %, B ≤ 95 %,
C the tail). XYZ measures demand regularity via the coefficient of
variation over the trailing 12 monthly buckets (zeros included): X ≤ 0.5,
Y ≤ 1.0, Z above (or no usage at all). ``velocity_score`` blends value and
hit-frequency percentiles — the optimizer's ranking key.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from ..store import open_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trailing_periods(n: int = 12, anchor: str | None = None) -> list[str]:
    if anchor:
        year, month = int(anchor[:4]), int(anchor[5:7])
    else:
        today = datetime.now(timezone.utc)
        year, month = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


def compute_velocity() -> dict[str, Any]:
    cn = open_conn()
    try:
        latest = cn.execute(
            "SELECT MAX(period) AS p FROM usage_history").fetchone()["p"]
        periods = _trailing_periods(12, latest)
        qmarks = ",".join("?" * len(periods))
        usage_rows = cn.execute(
            f"SELECT part_number, period, SUM(qty) AS qty FROM usage_history"
            f" WHERE period IN ({qmarks}) AND kind='issue'"
            f" GROUP BY part_number, period", periods).fetchall()
        cost = {r["part_number"]: r["unit_cost"] or 0.0 for r in cn.execute(
            "SELECT part_number, unit_cost FROM parts")}
        onhand = {r["part_number"]: r["qty"] or 0.0 for r in cn.execute(
            "SELECT part_number, SUM(qty_oh) AS qty FROM inventory"
            " GROUP BY part_number")}

        monthly: dict[str, dict[str, float]] = {}
        for r in usage_rows:
            monthly.setdefault(r["part_number"], {})[r["period"]] = float(r["qty"])

        stats: list[dict[str, Any]] = []
        for pn in set(list(monthly) + list(onhand)):
            buckets = [monthly.get(pn, {}).get(p, 0.0) for p in periods]
            qty12 = sum(buckets)
            hits = sum(1 for b in buckets if b > 0)
            mean = qty12 / 12.0
            std = math.sqrt(sum((b - mean) ** 2 for b in buckets) / 12.0)
            cv = (std / mean) if mean > 0 else None
            mos = (onhand.get(pn, 0.0) / mean) if mean > 0 else None
            stats.append({
                "part_number": pn, "qty12": qty12,
                "value12": qty12 * cost.get(pn, 0.0), "hits": hits,
                "cv": cv, "mos": mos,
            })

        # ABC on cumulative value share (share *entering* the item, so the
        # single biggest part is always A even at >80 % of total value)
        stats.sort(key=lambda s: -s["value12"])
        total_value = sum(s["value12"] for s in stats) or 1.0
        cum = 0.0
        for s in stats:
            share_before = cum / total_value
            cum += s["value12"]
            s["abc"] = "A" if share_before < 0.80 else (
                "B" if share_before < 0.95 else "C")
            if s["value12"] <= 0:
                s["abc"] = "C"
        # XYZ on CV
        for s in stats:
            if s["cv"] is None:
                s["xyz"] = "Z"
            elif s["cv"] <= 0.5:
                s["xyz"] = "X"
            elif s["cv"] <= 1.0:
                s["xyz"] = "Y"
            else:
                s["xyz"] = "Z"
        # score = 0.7·value percentile + 0.3·hits percentile
        n = max(len(stats), 1)
        by_hits = sorted(stats, key=lambda s: s["hits"])
        hit_rank = {s["part_number"]: i for i, s in enumerate(by_hits)}
        for i, s in enumerate(stats):  # stats still value-desc
            value_pct = 1.0 - i / n
            hits_pct = (hit_rank[s["part_number"]] + 1) / n
            s["score"] = round(0.7 * value_pct + 0.3 * hits_pct, 4)

        now = _now()
        for s in stats:
            cn.execute(
                "INSERT INTO velocity(part_number, usage_12m_qty, usage_12m_value,"
                " monthly_hits, abc_class, xyz_class, velocity_score,"
                " months_of_supply, computed_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(part_number) DO UPDATE SET"
                " usage_12m_qty=excluded.usage_12m_qty,"
                " usage_12m_value=excluded.usage_12m_value,"
                " monthly_hits=excluded.monthly_hits,"
                " abc_class=excluded.abc_class, xyz_class=excluded.xyz_class,"
                " velocity_score=excluded.velocity_score,"
                " months_of_supply=excluded.months_of_supply,"
                " computed_at=excluded.computed_at",
                (s["part_number"], s["qty12"], round(s["value12"], 2), s["hits"],
                 s["abc"], s["xyz"], s["score"],
                 round(s["mos"], 2) if s["mos"] is not None else None, now))
        cn.commit()
        classes: dict[str, int] = {}
        for s in stats:
            key = s["abc"] + s["xyz"]
            classes[key] = classes.get(key, 0) + 1
        return {"parts": len(stats), "classes": classes,
                "anchor_period": periods[0]}
    finally:
        cn.close()


def list_velocity() -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        return [dict(r) for r in cn.execute(
            "SELECT * FROM velocity ORDER BY velocity_score DESC")]
    finally:
        cn.close()
