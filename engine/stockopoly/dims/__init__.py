"""Dimension graph CRUD + solve orchestration over ``stockopoly.sqlite``.

Entities own per-axis variables (L/W/H/SPAN); measurements relate variables
to each other, to photo scales, or to absolute lengths. ``solve_all()``
feeds the whole graph to :mod:`stockopoly.dims.solver`, persists values +
confidence intervals back into ``dim_variables`` / ``photo_scales``, flags
outlier measurements, and reports ungrounded components so the UI can ask
for a reference shot.

All lengths are stored in **inches**; ``unit=`` on input converts.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from .. import events
from ..store import open_conn
from .solver import Measurement, solve as _solve

__all__ = [
    "AXES", "MEASUREMENT_KINDS", "UNIT_TO_IN",
    "create_entity", "ensure_variable", "add_measurement", "delete_measurement",
    "add_reference_object", "apply_reference", "list_reference_objects",
    "solve_all", "list_entities", "entity_detail", "list_measurements",
]

AXES = ("L", "W", "H", "SPAN")
MEASUREMENT_KINDS = ("absolute", "pixel_extent", "ratio", "identity",
                     "pattern_count", "sum_parts", "distance_estimate")
UNIT_TO_IN = {"in": 1.0, "ft": 12.0, "mm": 1.0 / 25.4, "cm": 1.0 / 2.54,
              "m": 39.3700787402}

# Default log-space sigma by measurement source (≈ relative error).
_SOURCE_SIGMA_LN = {"manual": 0.02, "ocr": 0.05, "pattern": 0.03,
                    "exif": 0.30, "llm": 0.15, "reference": 0.01}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_inches(value: float, unit: str) -> float:
    try:
        return float(value) * UNIT_TO_IN[unit]
    except KeyError:
        raise ValueError(f"Unknown unit {unit!r} (use {sorted(UNIT_TO_IN)})") from None


# ─────────────────────────────────────────────────────────── entities + vars
def create_entity(kind: str, label: str, *, group_id: str | None = None,
                  photo_id: str | None = None, notes: str = "") -> int:
    cn = open_conn()
    try:
        cur = cn.execute(
            "INSERT INTO dim_entities(kind, label, group_id, photo_id, notes,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (kind, label, group_id, photo_id, notes, _now()))
        cn.commit()
        return int(cur.lastrowid)
    finally:
        cn.close()


def ensure_variable(entity_id: int, axis: str) -> int:
    if axis not in AXES:
        raise ValueError(f"Axis must be one of {AXES}")
    cn = open_conn()
    try:
        row = cn.execute(
            "SELECT var_id FROM dim_variables WHERE entity_id=? AND axis=?",
            (entity_id, axis)).fetchone()
        if row:
            return int(row["var_id"])
        cur = cn.execute(
            "INSERT INTO dim_variables(entity_id, axis) VALUES (?,?)",
            (entity_id, axis))
        cn.commit()
        return int(cur.lastrowid)
    finally:
        cn.close()


# ─────────────────────────────────────────────────────────────── measurements
def add_measurement(kind: str, *, a_var: int, value: float,
                    b_var: int | None = None, photo_id: str | None = None,
                    part_vars: list[int] | None = None,
                    sigma: float | None = None, unit: str = "in",
                    source: str = "manual", meta: dict | None = None) -> int:
    """Insert one measurement (length inputs converted to inches).

    ``sigma`` semantics: absolute/distance/sum_parts → same unit as value;
    ratio/pattern → relative; pixel_extent → pixels. Defaults derive from
    ``source`` when omitted.
    """
    if kind not in MEASUREMENT_KINDS:
        raise ValueError(f"Unknown measurement kind {kind!r}")
    if kind in ("ratio", "identity", "pattern_count") and b_var is None:
        raise ValueError(f"{kind} requires b_var")
    if kind == "pixel_extent" and not photo_id:
        raise ValueError("pixel_extent requires photo_id")
    if kind == "sum_parts" and not part_vars:
        raise ValueError("sum_parts requires part_vars")

    stored_value = float(value)
    stored_sigma = sigma
    if kind in ("absolute", "distance_estimate"):
        stored_value = _to_inches(value, unit)
        if sigma is not None:
            stored_sigma = _to_inches(sigma, unit)
    elif kind == "sum_parts":
        # value is informational (expected total, 0 = unknown); sigma in inches
        stored_value = _to_inches(value, unit) if value else 0.0
        if sigma is not None:
            stored_sigma = _to_inches(sigma, unit)

    cn = open_conn()
    try:
        for vid in [a_var, b_var, *(part_vars or [])]:
            if vid is not None and cn.execute(
                    "SELECT 1 FROM dim_variables WHERE var_id=?", (vid,)).fetchone() is None:
                raise KeyError(f"Unknown var_id {vid}")
        cur = cn.execute(
            "INSERT INTO dim_measurements(kind, photo_id, a_var, b_var,"
            " part_vars_json, value, sigma, unit, source, meta_json, outlier,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
            (kind, photo_id, a_var, b_var,
             json.dumps(part_vars or [], separators=(",", ":")),
             stored_value, stored_sigma, "in" if kind != "pixel_extent" else "px",
             source, json.dumps(meta or {}, separators=(",", ":")), _now()))
        cn.commit()
        return int(cur.lastrowid)
    finally:
        cn.close()


def delete_measurement(meas_id: int) -> None:
    cn = open_conn()
    try:
        cn.execute("DELETE FROM dim_measurements WHERE meas_id=?", (meas_id,))
        cn.commit()
    finally:
        cn.close()


# ──────────────────────────────────────────────────────── reference objects
def add_reference_object(name: str, kind: str, *, dim_l: float | None = None,
                         dim_w: float | None = None, dim_h: float | None = None,
                         unit: str = "in", sigma_pct: float = 1.0,
                         notes: str = "") -> int:
    cn = open_conn()
    try:
        cur = cn.execute(
            "INSERT INTO reference_objects(name, kind, dim_l, dim_w, dim_h,"
            " unit, sigma_pct, notes) VALUES (?,?,?,?,?,'in',?,?)",
            (name, kind,
             _to_inches(dim_l, unit) if dim_l else None,
             _to_inches(dim_w, unit) if dim_w else None,
             _to_inches(dim_h, unit) if dim_h else None,
             sigma_pct, notes))
        cn.commit()
        return int(cur.lastrowid)
    finally:
        cn.close()


def list_reference_objects() -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        return [dict(r) for r in cn.execute(
            "SELECT * FROM reference_objects ORDER BY ref_id")]
    finally:
        cn.close()


def apply_reference(ref_id: int, *, photo_id: str,
                    pixel_extents: dict[str, float]) -> dict[str, Any]:
    """Ground a photo's scale with a known reference object visible in it.

    ``pixel_extents`` maps axis → measured pixel span of the reference in
    this photo (e.g. ``{"L": 612}``). Creates the reference entity (once),
    absolute measurements for its known dims, and pixel_extent measurements
    tying them to the photo scale.
    """
    cn = open_conn()
    try:
        ref = cn.execute("SELECT * FROM reference_objects WHERE ref_id=?",
                         (ref_id,)).fetchone()
        if ref is None:
            raise KeyError(f"Unknown reference object {ref_id}")
    finally:
        cn.close()

    entity_id = create_entity("object", f"ref:{ref['name']}", photo_id=photo_id,
                              notes="reference object")
    sigma_rel = max(float(ref["sigma_pct"] or 1.0), 0.1) / 100.0
    made = {"entity_id": entity_id, "measurements": []}
    for axis, col in (("L", "dim_l"), ("W", "dim_w"), ("H", "dim_h")):
        known = ref[col]
        if known is None:
            continue
        var_id = ensure_variable(entity_id, axis)
        m1 = add_measurement("absolute", a_var=var_id, value=float(known),
                             sigma=float(known) * sigma_rel, source="reference",
                             meta={"ref_id": ref_id})
        made["measurements"].append(m1)
        px = pixel_extents.get(axis)
        if px:
            m2 = add_measurement("pixel_extent", a_var=var_id, value=float(px),
                                 photo_id=photo_id, source="reference",
                                 meta={"ref_id": ref_id})
            made["measurements"].append(m2)
    return made


# ───────────────────────────────────────────────────────────────────── solve
def _sigma_ln_for(row) -> float:
    base = _SOURCE_SIGMA_LN.get(row["source"] or "manual", 0.05)
    sigma, value, kind = row["sigma"], row["value"], row["kind"]
    if kind == "identity":
        return 0.005
    if sigma and value and kind in ("absolute", "distance_estimate", "ratio",
                                    "pattern_count", "pixel_extent"):
        return max(float(sigma) / float(value), 1e-4)
    if kind == "distance_estimate":
        return max(base, 0.2)
    return base


def solve_all() -> dict[str, Any]:
    cn = open_conn()
    try:
        var_rows = cn.execute(
            "SELECT v.var_id, v.entity_id, v.axis, e.label, e.kind"
            " FROM dim_variables v JOIN dim_entities e ON e.entity_id=v.entity_id"
        ).fetchall()
        meas_rows = cn.execute("SELECT * FROM dim_measurements").fetchall()
    finally:
        cn.close()

    var_key = {r["var_id"]: f"v{r['var_id']}" for r in var_rows}
    photo_keys: dict[str, str] = {}
    measurements: list[Measurement] = []
    for r in meas_rows:
        kind = r["kind"]
        a = var_key.get(r["a_var"])
        if a is None:
            continue
        m = Measurement(kind=kind, a=a, value=float(r["value"] or 0.0),
                        meas_id=r["meas_id"], sigma_ln=_sigma_ln_for(r))
        if kind in ("ratio", "identity", "pattern_count"):
            m.b = var_key.get(r["b_var"])
            if m.b is None:
                continue
        elif kind == "pixel_extent":
            pid = r["photo_id"] or ""
            m.photo = photo_keys.setdefault(pid, f"p::{pid}")
        elif kind == "sum_parts":
            try:
                parts = json.loads(r["part_vars_json"] or "[]")
            except json.JSONDecodeError:
                parts = []
            m.parts = [var_key[p] for p in parts if p in var_key]
            if not m.parts:
                continue
            m.sigma_abs = float(r["sigma"]) if r["sigma"] else None
        measurements.append(m)

    result = _solve(list(var_key.values()), list(photo_keys.values()), measurements)

    solved_at = _now()
    cn = open_conn()
    try:
        for r in var_rows:
            key = var_key[r["var_id"]]
            if key not in result.values:
                continue
            val = result.values[key]
            s_ln = result.sigmas_ln.get(key, float("inf"))
            grounded = result.grounded.get(key, False)
            finite = math.isfinite(s_ln)
            conf = round(math.exp(-s_ln), 4) if (grounded and finite) else 0.0
            cn.execute(
                "UPDATE dim_variables SET value=?, value_ln=?, sigma_ln=?,"
                " ci_low=?, ci_high=?, confidence=?, solved_at=? WHERE var_id=?",
                (round(val, 4), math.log(val), round(s_ln, 6) if finite else None,
                 round(val * math.exp(-1.96 * s_ln), 4) if finite else None,
                 round(val * math.exp(1.96 * s_ln), 4) if finite else None,
                 conf, solved_at, r["var_id"]))
        for pid, key in photo_keys.items():
            if key not in result.scales:
                continue
            s_ln = result.scale_sigmas_ln.get(key, float("inf"))
            cn.execute(
                "INSERT INTO photo_scales(photo_id, plane, scale_ln, sigma_ln,"
                " solved_at) VALUES (?,?,?,?,?) ON CONFLICT(photo_id, plane)"
                " DO UPDATE SET scale_ln=excluded.scale_ln,"
                " sigma_ln=excluded.sigma_ln, solved_at=excluded.solved_at",
                (pid, "default", math.log(result.scales[key]),
                 round(s_ln, 6) if math.isfinite(s_ln) else None, solved_at))
        cn.execute("UPDATE dim_measurements SET outlier=0")
        for mid in result.outliers:
            cn.execute("UPDATE dim_measurements SET outlier=1 WHERE meas_id=?", (mid,))
        cn.commit()
    finally:
        cn.close()

    label_of = {var_key[r["var_id"]]: f"{r['label']}.{r['axis']}" for r in var_rows}
    components = [{
        "grounded": c["grounded"],
        "anchor_count": c["anchor_count"],
        "members": [label_of.get(k, k) for k in c["members"]],
    } for c in result.components]
    ungrounded = [c for c in components if not c["grounded"]]

    report = {
        "variables": len(var_rows),
        "photo_scales": len(photo_keys),
        "measurements": len(measurements),
        "outliers": result.outliers,
        "iterations": result.iterations,
        "rms": result.rms,
        "components": components,
        "ungrounded_components": len(ungrounded),
        "solved_at": solved_at,
    }
    events.record(
        "stockopoly_dims_solved",
        {k: report[k] for k in ("variables", "measurements", "outliers",
                                "ungrounded_components", "rms")},
        title=f"Dims solved: {len(var_rows)} vars, {len(measurements)} meas,"
              f" {len(ungrounded)} ungrounded",
        signal=0.7 if not ungrounded else 0.45)
    return report


# ──────────────────────────────────────────────────────────────────── queries
def list_entities() -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        ents = [dict(r) for r in cn.execute(
            "SELECT * FROM dim_entities ORDER BY entity_id")]
        for e in ents:
            e["variables"] = [dict(v) for v in cn.execute(
                "SELECT var_id, axis, value, sigma_ln, ci_low, ci_high,"
                " confidence, solved_at FROM dim_variables WHERE entity_id=?"
                " ORDER BY axis", (e["entity_id"],))]
        return ents
    finally:
        cn.close()


def entity_detail(entity_id: int) -> dict[str, Any]:
    cn = open_conn()
    try:
        row = cn.execute("SELECT * FROM dim_entities WHERE entity_id=?",
                         (entity_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown entity {entity_id}")
        out = dict(row)
        out["variables"] = [dict(v) for v in cn.execute(
            "SELECT * FROM dim_variables WHERE entity_id=? ORDER BY axis",
            (entity_id,))]
        var_ids = [v["var_id"] for v in out["variables"]]
        if var_ids:
            qs = ",".join("?" * len(var_ids))
            out["measurements"] = [dict(m) for m in cn.execute(
                f"SELECT * FROM dim_measurements WHERE a_var IN ({qs})"
                f" OR b_var IN ({qs}) ORDER BY meas_id", var_ids * 2)]
        else:
            out["measurements"] = []
        return out
    finally:
        cn.close()


def list_measurements(limit: int = 500) -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        return [dict(r) for r in cn.execute(
            "SELECT * FROM dim_measurements ORDER BY meas_id DESC LIMIT ?",
            (limit,))]
    finally:
        cn.close()
