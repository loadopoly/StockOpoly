"""Location space — label grammar → graph → 3D coordinates → travel costs.

A warehouse's location codes (``A-01-2-B`` = aisle A, bay 1, level 2, bin B)
are parsed with the configurable ``location_grammar`` regex (named groups
``aisle``/``bay``/``level``, optional ``bin``). Registered locations get 3D
boxes: aisles march along +x at a pitch of two rack depths plus the clear
aisle width, bays along +y, levels along +z, and sibling bins split their
bay's width. Physical sizes come from solved dimension entities named
``bay_width`` / ``rack_depth`` / ``level_height`` when the solver has
grounded them confidently, else from settings defaults — photo-derived
dimensions literally shape the map.

Travel cost is Dijkstra over a small graph (location → its aisle-front node
→ cross-aisle → dock), so blocking an aisle later only means dropping edges.
"""
from __future__ import annotations

import heapq
import json
import re
from datetime import datetime, timezone
from typing import Any

from .. import settings
from ..store import open_conn

__all__ = [
    "parse_label", "register_locations", "compute_coordinates",
    "travel_costs", "list_locations", "set_dock",
]

_AISLE_CLEAR_IN = 120.0   # clear aisle width between rack faces
_LEVEL_LIFT_COST = 30.0   # added travel-cost inches per level above 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_label(code: str, grammar: str | None = None) -> dict[str, Any] | None:
    """Parse one location code with the configured grammar; None if no match."""
    pattern = grammar or settings.get("location_grammar")
    try:
        m = re.match(pattern, code.strip().upper())
    except re.error:
        return None
    if not m:
        return None
    g = m.groupdict()
    if not (g.get("aisle") and g.get("bay") and g.get("level")):
        return None
    return {
        "aisle": g["aisle"],
        "bay": int(g["bay"]),
        "level": int(g["level"]),
        "bin": g.get("bin") or None,
    }


def register_locations(codes: list[str], *, zone: str | None = None,
                       source: str = "import", status: str = "active",
                       confidence: float = 1.0) -> dict[str, Any]:
    """Upsert location rows from raw codes. Unparseable codes are reported."""
    added, updated, failed = 0, 0, []
    cn = open_conn()
    try:
        grammar = settings.get("location_grammar", cn=cn)
        for code in codes:
            code_n = code.strip().upper()
            if not code_n:
                continue
            parsed = parse_label(code_n, grammar)
            if parsed is None:
                failed.append(code_n)
                continue
            exists = cn.execute("SELECT 1 FROM locations WHERE location_code=?",
                                (code_n,)).fetchone()
            cn.execute(
                "INSERT INTO locations(location_code, aisle, bay, level, bin,"
                " zone, status, source, confidence, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(location_code) DO UPDATE SET"
                " aisle=excluded.aisle, bay=excluded.bay, level=excluded.level,"
                " bin=excluded.bin, zone=COALESCE(excluded.zone, locations.zone),"
                " status=excluded.status, source=excluded.source,"
                " confidence=excluded.confidence, updated_at=excluded.updated_at",
                (code_n, parsed["aisle"], str(parsed["bay"]), parsed["level"],
                 parsed["bin"], zone, status, source, confidence, _now()))
            if exists:
                updated += 1
            else:
                added += 1
        cn.commit()
    finally:
        cn.close()
    return {"added": added, "updated": updated, "failed": failed}


def _solved_dim(cn, label: str, default: float, min_confidence: float = 0.5) -> float:
    """Prefer a confidently solved dimension entity over the settings default."""
    row = cn.execute(
        "SELECT v.value, v.confidence FROM dim_variables v"
        " JOIN dim_entities e ON e.entity_id=v.entity_id"
        " WHERE LOWER(e.label)=? AND v.value IS NOT NULL"
        " ORDER BY v.confidence DESC LIMIT 1", (label.lower(),)).fetchone()
    if row and row["value"] and (row["confidence"] or 0.0) >= min_confidence:
        return float(row["value"])
    return default


def compute_coordinates() -> dict[str, Any]:
    """Assign x/y/z/w/d/h + capacity to every parsed location; build layout."""
    cn = open_conn()
    try:
        cfg = settings.all_settings(cn=cn)
        bay_w = _solved_dim(cn, "bay_width", float(cfg["default_bay_width_in"]))
        rack_d = _solved_dim(cn, "rack_depth", float(cfg["default_rack_depth_in"]))
        level_h = _solved_dim(cn, "level_height", float(cfg["default_level_height_in"]))
        fill = float(cfg["fill_factor"])

        rows = cn.execute(
            "SELECT location_code, aisle, bay, level, bin FROM locations").fetchall()
        aisles = sorted({r["aisle"] for r in rows})
        aisle_x = {a: i * (2 * rack_d + _AISLE_CLEAR_IN) for i, a in enumerate(aisles)}

        # bins per (aisle, bay, level) → slot widths
        bins_in_bay: dict[tuple, list] = {}
        for r in rows:
            bins_in_bay.setdefault((r["aisle"], int(r["bay"]), r["level"]),
                                   []).append(r["bin"] or "")
        located = 0
        for r in rows:
            key = (r["aisle"], int(r["bay"]), r["level"])
            siblings = sorted(set(bins_in_bay[key]))
            n_bins = max(len(siblings), 1)
            slot = siblings.index(r["bin"] or "")
            w = bay_w / n_bins
            x = aisle_x[r["aisle"]]
            y = (int(r["bay"]) - 1) * bay_w + (slot + 0.5) * w
            z = (int(r["level"]) - 1) * level_h
            cap = round(w * rack_d * level_h, 2)
            cn.execute(
                "UPDATE locations SET x=?, y=?, z=?, w=?, d=?, h=?,"
                " capacity_volume=?, fill_factor=? WHERE location_code=?",
                (round(x, 2), round(y, 2), round(z, 2), round(w, 2),
                 round(rack_d, 2), round(level_h, 2), cap, fill,
                 r["location_code"]))
            located += 1

        for a in aisles:
            max_bay = max(int(r["bay"]) for r in rows if r["aisle"] == a)
            cn.execute(
                "INSERT INTO layout(kind, code, origin_x, origin_y,"
                " orientation_deg, length, width, meta_json) VALUES"
                " ('aisle',?,?,?,0,?,?,?) ON CONFLICT(kind, code) DO UPDATE SET"
                " origin_x=excluded.origin_x, length=excluded.length,"
                " width=excluded.width, meta_json=excluded.meta_json",
                (a, round(aisle_x[a], 2), 0.0, round(max_bay * bay_w, 2),
                 round(2 * rack_d + _AISLE_CLEAR_IN, 2),
                 json.dumps({"bays": max_bay})))
        dock_code = str(cfg["dock_code"])
        cn.execute(
            "INSERT INTO layout(kind, code, origin_x, origin_y, orientation_deg,"
            " length, width, meta_json) VALUES ('dock',?,?,?,0,?,?,'{}')"
            " ON CONFLICT(kind, code) DO NOTHING",
            (dock_code, -_AISLE_CLEAR_IN, -_AISLE_CLEAR_IN, 144.0, 144.0))
        cn.commit()
    finally:
        cn.close()
    return {"located": located, "aisles": len(aisles),
            "bay_width_in": bay_w, "rack_depth_in": rack_d,
            "level_height_in": level_h}


def _dock_origin(cn) -> tuple[float, float]:
    row = cn.execute("SELECT origin_x, origin_y FROM layout WHERE kind='dock'"
                     " LIMIT 1").fetchone()
    if row:
        return float(row["origin_x"]), float(row["origin_y"])
    return -_AISLE_CLEAR_IN, -_AISLE_CLEAR_IN


def set_dock(x: float, y: float, code: str | None = None) -> None:
    cn = open_conn()
    try:
        dock_code = code or str(settings.get("dock_code", cn=cn))
        cn.execute(
            "INSERT INTO layout(kind, code, origin_x, origin_y, orientation_deg,"
            " length, width, meta_json) VALUES ('dock',?,?,?,0,144,144,'{}')"
            " ON CONFLICT(kind, code) DO UPDATE SET origin_x=excluded.origin_x,"
            " origin_y=excluded.origin_y", (dock_code, x, y))
        cn.commit()
    finally:
        cn.close()


def travel_costs() -> dict[str, float]:
    """Dijkstra from the dock through aisle-front nodes to every location.

    Edge model: dock ↔ each aisle front (cross-aisle distance), aisle front ↔
    location (distance down the aisle + lift penalty per level above 1).
    Returns inches of travel per location_code.
    """
    cn = open_conn()
    try:
        rows = cn.execute(
            "SELECT location_code, aisle, x, y, z, level FROM locations"
            " WHERE x IS NOT NULL AND status != 'blocked'").fetchall()
        dock_x, dock_y = _dock_origin(cn)
    finally:
        cn.close()
    if not rows:
        return {}

    graph: dict[str, list[tuple[str, float]]] = {"__dock__": []}
    aisle_front: dict[str, tuple[float, float]] = {}
    for r in rows:
        aisle_front.setdefault(r["aisle"], (float(r["x"]), 0.0))
    for a, (ax, ay) in aisle_front.items():
        node = f"__aisle__{a}"
        cost = abs(ax - dock_x) + abs(ay - dock_y)
        graph["__dock__"].append((node, cost))
        graph.setdefault(node, []).append(("__dock__", cost))
    for r in rows:
        node = f"__aisle__{r['aisle']}"
        lift = max(int(r["level"] or 1) - 1, 0) * _LEVEL_LIFT_COST
        cost = abs(float(r["y"])) + lift
        graph.setdefault(node, []).append((r["location_code"], cost))
        graph.setdefault(r["location_code"], []).append((node, cost))

    dist: dict[str, float] = {"__dock__": 0.0}
    pq: list[tuple[float, str]] = [(0.0, "__dock__")]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")):
            continue
        for v, w in graph.get(u, []):
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return {r["location_code"]: round(dist[r["location_code"]], 1)
            for r in rows if r["location_code"] in dist}


def list_locations(zone: str | None = None) -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        if zone:
            rows = cn.execute("SELECT * FROM locations WHERE zone=?"
                              " ORDER BY aisle, CAST(bay AS INTEGER), level, bin",
                              (zone,)).fetchall()
        else:
            rows = cn.execute("SELECT * FROM locations"
                              " ORDER BY aisle, CAST(bay AS INTEGER), level, bin"
                              ).fetchall()
        return [dict(r) for r in rows]
    finally:
        cn.close()
