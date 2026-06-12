"""Current-state occupancy: how full is every location, really.

Unit volume preference order: explicit part dims → normalized container
volume / units-per-container → ``default_unit_volume_in3`` setting. Status
bands: over (> capacity·fill) / tight (> 85 % of usable) / ok / empty.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .. import settings
from ..store import open_conn


def _unit_volumes(cn, default_v: float) -> dict[str, float]:
    out: dict[str, float] = {}
    boxes = {r["box_id"]: float(r["l"] * r["w"] * r["h"]) for r in cn.execute(
        "SELECT box_id, l, w, h FROM std_containers")}
    for r in cn.execute("SELECT part_number, dim_l, dim_w, dim_h, norm_box_id,"
                        " norm_qty_per_box FROM parts"):
        if r["dim_l"] and r["dim_w"] and r["dim_h"]:
            out[r["part_number"]] = float(r["dim_l"] * r["dim_w"] * r["dim_h"])
        elif r["norm_box_id"] in boxes and (r["norm_qty_per_box"] or 0) > 0:
            out[r["part_number"]] = boxes[r["norm_box_id"]] / float(r["norm_qty_per_box"])
        else:
            out[r["part_number"]] = default_v
    return out


def compute_occupancy() -> dict[str, Any]:
    cn = open_conn()
    try:
        default_v = float(settings.get("default_unit_volume_in3", cn=cn))
        unit_v = _unit_volumes(cn, default_v)
        caps = {r["location_code"]: (float(r["capacity_volume"] or 0.0),
                                     float(r["fill_factor"] or 0.85))
                for r in cn.execute(
                    "SELECT location_code, capacity_volume, fill_factor"
                    " FROM locations")}
        used: dict[str, float] = {}
        parts_in: dict[str, set] = {}
        for r in cn.execute("SELECT part_number, location_code, qty_oh"
                            " FROM inventory WHERE qty_oh > 0"):
            loc = r["location_code"]
            used[loc] = used.get(loc, 0.0) + float(r["qty_oh"]) * \
                unit_v.get(r["part_number"], default_v)
            parts_in.setdefault(loc, set()).add(r["part_number"])

        now = datetime.now(timezone.utc).isoformat()
        counts = {"over": 0, "tight": 0, "ok": 0, "empty": 0}
        all_locs = set(caps) | set(used)
        for loc in all_locs:
            cap, fill = caps.get(loc, (0.0, 0.85))
            usable = cap * fill
            u = used.get(loc, 0.0)
            if u <= 0:
                status = "empty"
                pct = 0.0
            elif usable <= 0:
                status = "over"      # stock in an unmeasured/zero-cap location
                pct = 1.0
            else:
                pct = u / usable
                status = "over" if pct > 1.0 else ("tight" if pct > 0.85 else "ok")
            counts[status] += 1
            cn.execute(
                "INSERT INTO occupancy(location_code, used_volume,"
                " capacity_volume, occupancy_pct, part_count, status,"
                " computed_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(location_code) DO UPDATE SET"
                " used_volume=excluded.used_volume,"
                " capacity_volume=excluded.capacity_volume,"
                " occupancy_pct=excluded.occupancy_pct,"
                " part_count=excluded.part_count, status=excluded.status,"
                " computed_at=excluded.computed_at",
                (loc, round(u, 1), cap, round(pct, 4),
                 len(parts_in.get(loc, ())), status, now))
        cn.commit()
        return {"locations": len(all_locs), **counts}
    finally:
        cn.close()


def current_state(status: str | None = None) -> list[dict[str, Any]]:
    """Occupancy rows joined with their inventory lines (UI list view)."""
    cn = open_conn()
    try:
        sql = ("SELECT o.*, l.aisle, l.bay, l.level, l.bin, l.zone"
               " FROM occupancy o LEFT JOIN locations l"
               " ON l.location_code=o.location_code")
        args: tuple = ()
        if status:
            sql += " WHERE o.status=?"
            args = (status,)
        rows = [dict(r) for r in cn.execute(sql + " ORDER BY o.occupancy_pct DESC",
                                            args)]
        for row in rows:
            row["inventory"] = [dict(i) for i in cn.execute(
                "SELECT part_number, qty_oh, uom FROM inventory"
                " WHERE location_code=? AND qty_oh>0 ORDER BY qty_oh DESC",
                (row["location_code"],))]
        return rows
    finally:
        cn.close()
