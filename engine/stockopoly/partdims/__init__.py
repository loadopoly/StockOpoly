"""Part-dimension utilities: standard containers + carton normalization.

``seed_std_containers()`` installs the house set of cartons/totes/pallets;
``normalize_all()`` assigns every dimensioned part its smallest standard
container and units-per-container (best of the six axis orientations), which
the slotting optimizer uses to convert quantities into shelf volume.
"""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import permutations
from typing import Any

from ..store import open_conn

__all__ = ["seed_std_containers", "fit_qty", "normalize_part", "normalize_all",
           "list_std_containers"]

# (box_id, kind, name, L, W, H, max_weight_lb)
_STD = [
    ("carton_s", "carton", "Small carton", 12.0, 10.0, 8.0, 40.0),
    ("carton_m", "carton", "Medium carton", 18.0, 14.0, 12.0, 60.0),
    ("carton_l", "carton", "Large carton", 24.0, 18.0, 18.0, 80.0),
    ("tote", "tote", "Warehouse tote", 27.0, 17.0, 12.0, 70.0),
    ("gaylord", "carton", "Gaylord box", 48.0, 40.0, 36.0, 1500.0),
    ("pallet_gma", "pallet", "GMA pallet load", 48.0, 40.0, 60.0, 2500.0),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_std_containers() -> int:
    cn = open_conn()
    try:
        for box in _STD:
            cn.execute(
                "INSERT OR IGNORE INTO std_containers(box_id, kind, name, l, w,"
                " h, max_weight, unit) VALUES (?,?,?,?,?,?,?,'in')", box)
        cn.commit()
        return cn.execute("SELECT COUNT(*) FROM std_containers").fetchone()[0]
    finally:
        cn.close()


def list_std_containers() -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        return [dict(r) for r in cn.execute(
            "SELECT * FROM std_containers ORDER BY l*w*h")]
    finally:
        cn.close()


def fit_qty(part_dims: tuple[float, float, float],
            box_dims: tuple[float, float, float]) -> int:
    """Units of an axis-aligned part per box — best of the 6 orientations."""
    if min(part_dims) <= 0 or min(box_dims) <= 0:
        return 0
    best = 0
    for perm in permutations(part_dims):
        q = 1
        for p, b in zip(perm, box_dims):
            q *= int(b // p)
        best = max(best, q)
    return best


def normalize_part(part_number: str) -> dict[str, Any] | None:
    """Pick the smallest std container that fits ≥1 unit; persist on the part."""
    cn = open_conn()
    try:
        part = cn.execute("SELECT part_number, dim_l, dim_w, dim_h, weight"
                          " FROM parts WHERE part_number=?",
                          (part_number,)).fetchone()
        if part is None:
            raise KeyError(f"Unknown part {part_number}")
        if not (part["dim_l"] and part["dim_w"] and part["dim_h"]):
            return None
        dims = (float(part["dim_l"]), float(part["dim_w"]), float(part["dim_h"]))
        boxes = cn.execute("SELECT * FROM std_containers ORDER BY l*w*h").fetchall()
        for box in boxes:
            qty = fit_qty(dims, (box["l"], box["w"], box["h"]))
            if qty < 1:
                continue
            if part["weight"] and box["max_weight"]:
                qty = min(qty, int(box["max_weight"] // float(part["weight"])) or 1)
            cn.execute("UPDATE parts SET norm_box_id=?, norm_qty_per_box=?,"
                       " updated_at=? WHERE part_number=?",
                       (box["box_id"], qty, _now(), part_number))
            cn.commit()
            return {"part_number": part_number, "box_id": box["box_id"],
                    "qty_per_box": qty}
        return None
    finally:
        cn.close()


def normalize_all() -> dict[str, Any]:
    seed_std_containers()
    cn = open_conn()
    try:
        parts = [r["part_number"] for r in cn.execute(
            "SELECT part_number FROM parts")]
    finally:
        cn.close()
    normalized, missing = 0, 0
    for pn in parts:
        if normalize_part(pn):
            normalized += 1
        else:
            missing += 1
    return {"normalized": normalized, "missing_dims": missing}
