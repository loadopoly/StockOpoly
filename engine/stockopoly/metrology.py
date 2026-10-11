"""Photogrammetric connection — local standards, EXIF priors, shared photo keys.

Bridges the vendored :mod:`stockopoly.photometrology` contract
(``holon.metrology/1``, canonical in the umbrella repo's ``Metrology/``) into
the relational dimension solver:

* A **local standard** (a person's calibrated finger, palm, shoe — or any
  personal object) applied to a photo becomes a reference entity with an
  ``absolute`` length and a ``pixel_extent`` on that photo, so the WLS solver
  grounds the photo's scale exactly as it does for a carton or a tape.
  Personal standards apply only when the caller states the photo was taken by
  (and shows) the standard's owner.
* An **EXIF prior** (focus distance + 35 mm-equivalent focal length) adds a
  loose frame-width measurement on the focus plane. Opt-in
  (``metrology_exif_prior``) because it constrains the photo's single scale
  even when the reference sits on another plane.
* Every scale source is logged in ``metrology_observations`` under the
  photo's content key (``sha256:<hex>``) — the key GeoGraph OCR and any other
  app compute for the same JPEG. The public view of that table (no owner, no
  basis) mirrors to the shared Supabase ``metrology_observations`` table.
* ``export_error_stats`` writes unit-free error statistics — the only
  metrology that leaves this store for Perceptopoly/QUIPU.

Lengths in this engine are inches; the contract is millimetres. Conversion
happens here and nowhere else.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import dims, settings
from . import photometrology as pm
from .store import open_conn

__all__ = [
    "photo_key", "import_standards", "register_standard", "list_standards",
    "apply_standard", "apply_reference_key", "add_exif_prior", "exif_scale",
    "photo_report", "export_error_stats", "learned_params", "measure_edge", "apply_grid",
]

MM_PER_IN = 25.4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _photo(cn, photo_id: str):
    row = cn.execute("SELECT * FROM photos WHERE photo_id=?", (photo_id,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown photo {photo_id}")
    return row


def photo_key(photo_id: str) -> str:
    """The cross-app key for a stored photo (``sha256:`` of its bytes)."""
    cn = open_conn()
    try:
        row = _photo(cn, photo_id)
    finally:
        cn.close()
    if row["sha256"]:
        return "sha256:" + row["sha256"]
    return pm.photo_key(row["abs_path"])


def _log_obs(cn, *, photo_id: str, key: str, scale: pm.Scale, ref_kind: str,
             owner: str | None, pixels: float | None, pixel_sigma: float | None,
             meas_ids: list[int]) -> int:
    cur = cn.execute(
        "INSERT INTO metrology_observations(photo_key, photo_id, ref_kind, basis, owner,"
        " source_class, plane, pixels, pixel_sigma, ln_mm_per_px, sigma_ln, meas_ids_json,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, photo_id, ref_kind, scale.basis, owner, scale.source_class, scale.plane,
         pixels, pixel_sigma, scale.ln_mm_per_px, scale.sigma_ln, json.dumps(meas_ids),
         _now()))
    return int(cur.lastrowid)


# ─────────────────────────────────────────────────────────── standards
def register_standard(rec: dict[str, Any]) -> str:
    """Upsert one ``local_standard`` record (from ``Metrology/metrology_cli.py
    calibrate``). Returns its key ``owner:part``."""
    st = pm.LocalStandard.from_record(rec)
    cn = open_conn()
    try:
        cn.execute(
            "INSERT INTO metrology_standards(standard_key, owner, part, definition,"
            " value_mm, sigma_mm, method, calibrated_at, valid_until, privacy,"
            " evidence_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(standard_key) DO UPDATE SET definition=excluded.definition,"
            " value_mm=excluded.value_mm, sigma_mm=excluded.sigma_mm,"
            " method=excluded.method, calibrated_at=excluded.calibrated_at,"
            " valid_until=excluded.valid_until, privacy=excluded.privacy,"
            " evidence_json=excluded.evidence_json, updated_at=excluded.updated_at",
            (st.key, st.owner, st.part, st.definition, st.value_mm, st.sigma_mm,
             st.method, st.calibrated_at, st.valid_until, st.privacy,
             json.dumps(list(st.evidence), ensure_ascii=False), _now()))
        cn.commit()
    finally:
        cn.close()
    return st.key


def import_standards(path: str | Path) -> list[str]:
    recs = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(recs, dict):
        recs = [recs]
    return [register_standard(r) for r in recs if r.get("kind") == "local_standard"]


def _standard(cn, key: str) -> pm.LocalStandard:
    row = cn.execute("SELECT * FROM metrology_standards WHERE standard_key=?",
                     (key,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown standard {key}")
    return pm.LocalStandard(
        owner=row["owner"], part=row["part"], value_mm=row["value_mm"],
        sigma_mm=row["sigma_mm"], method=row["method"] or "",
        calibrated_at=row["calibrated_at"] or "", valid_until=row["valid_until"],
        definition=row["definition"] or "", privacy=row["privacy"] or "personal")


def list_standards(owner: str | None = None) -> list[dict[str, Any]]:
    """Standards without their evidence (frames/readings stay in the record)."""
    cn = open_conn()
    try:
        q = ("SELECT standard_key, owner, part, definition, value_mm, sigma_mm, method,"
             " calibrated_at, valid_until, privacy FROM metrology_standards")
        rows = cn.execute(q + (" WHERE owner=?" if owner else "") + " ORDER BY standard_key",
                          (owner,) if owner else ()).fetchall()
        return [dict(r) for r in rows]
    finally:
        cn.close()


def _ground(photo_id: str, label: str, length_mm: float, sigma_mm: float,
            pixels: float, pixel_sigma: float, source: str) -> tuple[int, list[int]]:
    """Reference entity + absolute length + pixel extent → solver grounding."""
    ent = dims.create_entity("object", label, photo_id=photo_id, notes="metrology reference")
    var = dims.ensure_variable(ent, "L")
    m1 = dims.add_measurement("absolute", a_var=var, value=length_mm, sigma=sigma_mm,
                              unit="mm", source=source, meta={"metrology": label})
    m2 = dims.add_measurement("pixel_extent", a_var=var, value=pixels, photo_id=photo_id,
                              sigma=pixel_sigma, source=source, meta={"metrology": label})
    return ent, [m1, m2]


def apply_standard(standard_key: str, photo_id: str, pixels: float, *,
                   pixel_sigma: float = 2.0, plane: str = "default",
                   photographed_owner: str | None = None) -> dict[str, Any]:
    """Use a local standard visible in a photo to ground that photo's scale.

    A personal standard is applied only when ``photographed_owner`` equals the
    standard's owner: one person's finger is never another person's ruler.
    """
    cn = open_conn()
    try:
        st = _standard(cn, standard_key)
        _photo(cn, photo_id)
    finally:
        cn.close()
    if st.privacy == "personal" and photographed_owner != st.owner:
        raise PermissionError(
            f"{standard_key} is personal to {st.owner}; pass photographed_owner="
            f"{st.owner!r} only if this photo shows {st.owner}'s own {st.part}")
    scale = pm.scale_from_span(st.value_mm, st.sigma_ln, pixels, pixel_sigma,
                               source_class="measured", basis=f"standard:{st.key}",
                               plane=plane)
    ent, meas = _ground(photo_id, f"std:{st.key}", st.value_mm, st.sigma_mm,
                        pixels, pixel_sigma, "reference")
    key = photo_key(photo_id)
    cn = open_conn()
    try:
        obs = _log_obs(cn, photo_id=photo_id, key=key, scale=scale, ref_kind="standard",
                       owner=st.owner, pixels=pixels, pixel_sigma=pixel_sigma, meas_ids=meas)
        cn.commit()
    finally:
        cn.close()
    return {"obs_id": obs, "entity_id": ent, "measurements": meas, "photo_key": key,
            "mm_per_px": round(scale.mm_per_px, 6), "rel_sigma": round(scale.rel_sigma, 4)}


def apply_reference_key(ref_key: str, photo_id: str, pixels: float, *,
                        pixel_sigma: float = 2.0, plane: str = "default") -> dict[str, Any]:
    """Same, for a published size in the shared catalogue (``pm.REFERENCES``)."""
    ref = pm.REFERENCES[ref_key]
    scale = pm.scale_from_span(ref.length_mm, ref.sigma_ln, pixels, pixel_sigma,
                               source_class="recorded", basis=f"reference:{ref.key}",
                               plane=plane)
    ent, meas = _ground(photo_id, f"ref:{ref.key}", ref.length_mm, ref.sigma_mm,
                        pixels, pixel_sigma, "reference")
    key = photo_key(photo_id)
    cn = open_conn()
    try:
        obs = _log_obs(cn, photo_id=photo_id, key=key, scale=scale, ref_kind="reference",
                       owner=None, pixels=pixels, pixel_sigma=pixel_sigma, meas_ids=meas)
        cn.commit()
    finally:
        cn.close()
    return {"obs_id": obs, "entity_id": ent, "measurements": meas, "photo_key": key,
            "mm_per_px": round(scale.mm_per_px, 6), "rel_sigma": round(scale.rel_sigma, 4)}


# ─────────────────────────────────────────────────────────── EXIF prior
def _raw_exif_scale(photo_id: str) -> tuple[pm.Scale | None, float | None]:
    """The camera's own prior, unadjusted — what is stored, so the field
    statistics always measure the raw estimator (else the learned bias would
    learn itself back to zero)."""
    cn = open_conn()
    try:
        row = _photo(cn, photo_id)
    finally:
        cn.close()
    try:
        meta = json.loads(row["exif_json"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    f35 = row["focal_35mm"] or meta.get("focal_35mm")
    dist = meta.get("focus_distance_m")
    if not (row["width"] and row["height"] and f35 and dist):
        return None, None
    return pm.scale_from_exif(int(row["width"]), int(row["height"]), float(f35), float(dist)), float(f35)


def learned_params() -> dict[str, Any]:
    """The dynamic calibration parameters in force right now.

    With ``$HOLON_METROLOGY_PARAMS`` set (the cross-app aggregate written by
    ``Metrology/metrology_cli.py params``), that file. Otherwise learned from
    this store's own photos that carry both an EXIF prior and an anchored
    scale. Either way every unknown starts at its prior.
    """
    import os
    if os.environ.get("HOLON_METROLOGY_PARAMS"):
        return pm.load_params()
    return pm.params_from_stats(_local_error_rows(stats_only=True))


def _apply_learned(raw: pm.Scale, lens: float | None, params: dict[str, Any]) -> pm.Scale:
    e = pm.exif_params(lens, params)
    return pm.Scale(raw.ln_mm_per_px - e["bias_ln"], e["sigma_ln"], raw.source_class,
                    "exif" if e["bias_ln"] == 0.0 else "exif:debiased", raw.plane)


def exif_scale(photo_id: str, *, learned: bool = True) -> pm.Scale | None:
    """EXIF prior for a photo; by default with the learned per-lens bias and
    uncertainty applied."""
    raw, lens = _raw_exif_scale(photo_id)
    if raw is None or not learned:
        return raw
    return _apply_learned(raw, lens, learned_params())


def add_exif_prior(photo_id: str, *, force: bool = False) -> dict[str, Any] | None:
    """Log the EXIF prior; also feed it to the solver when the setting allows."""
    raw, lens = _raw_exif_scale(photo_id)
    if raw is None:
        return None
    scale = _apply_learned(raw, lens, learned_params())
    key = photo_key(photo_id)
    meas: list[int] = []
    ent = None
    if force or settings.get("metrology_exif_prior"):
        cn = open_conn()
        try:
            row = _photo(cn, photo_id)
        finally:
            cn.close()
        short_px = float(min(row["width"], row["height"]))
        frame_mm = scale.mm_per_px * short_px
        # sigma chosen so sigma/value equals the prior's log sigma (solver convention)
        ent, meas = _ground(photo_id, f"exif_frame:{photo_id}", frame_mm,
                            frame_mm * scale.sigma_ln, short_px, 0.5, "exif")
    cn = open_conn()
    try:
        obs = _log_obs(cn, photo_id=photo_id, key=key, scale=raw, ref_kind="exif",
                       owner=None, pixels=None, pixel_sigma=None, meas_ids=meas)
        cn.commit()
    finally:
        cn.close()
    return {"obs_id": obs, "entity_id": ent, "photo_key": key,
            "mm_per_px": round(scale.mm_per_px, 6), "raw_mm_per_px": round(raw.mm_per_px, 6),
            "sigma_ln": round(scale.sigma_ln, 4), "in_solver": bool(meas)}


# ─────────────────────────────────────────────────────────── reports
def _obs_scales(cn, photo_id: str) -> list[pm.Scale]:
    rows = cn.execute("SELECT * FROM metrology_observations WHERE photo_id=?"
                      " ORDER BY obs_id", (photo_id,)).fetchall()
    return [pm.Scale(r["ln_mm_per_px"], r["sigma_ln"], r["source_class"], r["basis"] or "",
                     r["plane"] or "default") for r in rows]


def _local_error_rows(stats_only: bool = False) -> list[dict[str, Any]]:
    """Raw-EXIF vs anchored residuals for every photo that has both."""
    cn = open_conn()
    try:
        pids = [r["photo_id"] for r in cn.execute(
            "SELECT DISTINCT photo_id FROM metrology_observations WHERE ref_kind='exif'")]
        rows = []
        for pid in pids:
            scales = _obs_scales(cn, pid)
            ex = [s for s in scales if s.source_class == "projected"]
            an = [s for s in scales if s.source_class in ("measured", "recorded")]
            if not ex or not an:
                continue
            ph = cn.execute("SELECT focal_35mm FROM photos WHERE photo_id=?", (pid,)).fetchone()
            ref = pm.fuse(an)["scale"]
            rows.append({"estimator": "exif",
                         "lens": str(int(ph["focal_35mm"])) if ph and ph["focal_35mm"] else "",
                         "log_err": ex[0].ln_mm_per_px - ref.ln_mm_per_px})
    finally:
        cn.close()
    return pm.error_stats(rows) if stats_only else rows


# ─────────────────────────────────────────────── edges (dynamic Planck floor)
def measure_edge(photo_id: str, box: tuple[int, int, int, int], *,
                 axis: str = "vertical", plane: str = "default") -> dict[str, Any]:
    """Measure how sharply an edge in the photo can be located.

    ``box`` (x0, y0, x1, y1, display orientation) contains one straight edge;
    ``axis`` says which way the edge runs. Intensity profiles are taken across
    it, one per pixel row/column, and :func:`photometrology.edge_sigma_from_profiles`
    turns contrast, noise and blur into the edge's Cramér–Rao / quantum floor.
    Stored per photo; ``photo_report`` uses it for the Planck floor. Needs
    Pillow (optional accelerator) — raises RuntimeError without it.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("measure_edge needs Pillow (requirements-optional.txt)") from exc
    cn = open_conn()
    try:
        row = _photo(cn, photo_id)
    finally:
        cn.close()
    im = ImageOps.exif_transpose(Image.open(row["abs_path"])).convert("L")
    x0, y0, x1, y1 = (int(v) for v in box)
    crop = im.crop((x0, y0, x1, y1))
    w, h = crop.size
    px = crop.load()
    if axis == "vertical":     # edge runs top→bottom: profiles go left→right
        profiles = [[float(px[x, y]) for x in range(w)] for y in range(h)]
    elif axis == "horizontal":
        profiles = [[float(px[x, y]) for y in range(h)] for x in range(w)]
    else:
        raise ValueError("axis must be 'vertical' or 'horizontal'")
    res = pm.edge_sigma_from_profiles(profiles)
    cn = open_conn()
    try:
        cur = cn.execute(
            "INSERT INTO metrology_edges(photo_id, plane, box_json, axis, edge_sigma_px,"
            " cramer_rao_px, quantum_px, contrast, noise, psf_sigma_px, rows, limited_by,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (photo_id, plane, json.dumps([x0, y0, x1, y1]), axis, res["edge_sigma_px"],
             res.get("cramer_rao_px"), res.get("quantum_px"), res["contrast"], res["noise"],
             res["psf_sigma_px"], res["rows"], res["limited_by"], _now()))
        cn.commit()
        res["edge_id"] = int(cur.lastrowid)
    finally:
        cn.close()
    return res


def _edge_sigma(cn, photo_id: str) -> tuple[float, str]:
    """Best available edge sigma for a photo: measured edges (median), else
    the reading uncertainty people gave for spans on it (σ_span/√2), else 1 px
    flagged as assumed."""
    vals = sorted(r[0] for r in cn.execute(
        "SELECT edge_sigma_px FROM metrology_edges WHERE photo_id=? AND edge_sigma_px IS NOT NULL",
        (photo_id,)) if r[0] is not None and math.isfinite(r[0]))
    if vals:
        return vals[len(vals) // 2], f"measured ({len(vals)} edge{'s' if len(vals) > 1 else ''})"
    sp = sorted(r[0] / math.sqrt(2.0) for r in cn.execute(
        "SELECT pixel_sigma FROM metrology_observations WHERE photo_id=? AND pixel_sigma > 0",
        (photo_id,)))
    if sp:
        return sp[len(sp) // 2], "from span reading uncertainty"
    return 1.0, "assumed (no edge measured)"


# ─────────────────────────────────────────────── grids (pegboard, uprights)
def apply_grid(photo_id: str, points_px: list[tuple[float, float]],
               indices: list[tuple[int, int]], ref_key: str, *,
               plane: str = "default") -> dict[str, Any]:
    """Ground a photo's scale from a regular grid (pegboard holes, upright
    punches, wire deck) using the shared estimator and the *learned* grid
    uncertainty constant."""
    ref = pm.REFERENCES[ref_key]
    params = learned_params()
    g = pm.grid_scale(points_px, indices, ref.length_mm, unit="mm", k=pm.grid_k(params))
    s0 = g["scale"]
    scale = pm.Scale(s0.ln_mm_per_px, math.hypot(s0.sigma_ln, ref.sigma_ln), "recorded",
                     f"grid:{ref_key}", plane)
    # into the solver as one pitch spanning its equivalent pixels
    px_per_pitch = ref.length_mm / scale.mm_per_px
    ent, meas = _ground(photo_id, f"grid:{ref_key}", ref.length_mm,
                        ref.length_mm * scale.sigma_ln, px_per_pitch, 0.0001, "pattern")
    key = photo_key(photo_id)
    cn = open_conn()
    try:
        obs = _log_obs(cn, photo_id=photo_id, key=key, scale=scale, ref_kind="grid",
                       owner=None, pixels=px_per_pitch, pixel_sigma=None, meas_ids=meas)
        cn.commit()
    finally:
        cn.close()
    return {"obs_id": obs, "mm_per_px": round(scale.mm_per_px, 6),
            "rel_sigma": round(scale.rel_sigma, 5), "rms_px": g["rms_px"],
            "grid_k": pm.grid_k(params)}


def photo_report(photo_id: str, edge_sigma_px: float | None = None) -> dict[str, Any]:
    """Every scale source for a photo, their fusion, the solver's answer, and
    the Planck floor: the smallest length resolvable on the fused plane.

    All dynamic: EXIF sources carry the learned per-lens bias/uncertainty;
    the floor uses the photo's measured edge sharpness when there is one
    (``measure_edge``), else what the readings imply; ``edge_sigma_px``
    overrides."""
    params = learned_params()
    cn = open_conn()
    try:
        scales = _obs_scales(cn, photo_id)
        lens_row = cn.execute("SELECT focal_35mm FROM photos WHERE photo_id=?", (photo_id,)).fetchone()
        solved = cn.execute("SELECT scale_ln, sigma_ln FROM photo_scales WHERE photo_id=?"
                            " AND plane='default'", (photo_id,)).fetchone()
        if edge_sigma_px is None:
            edge_sigma_px, edge_src = _edge_sigma(cn, photo_id)
        else:
            edge_src = "given"
    finally:
        cn.close()
    lens = lens_row["focal_35mm"] if lens_row else None
    scales = [_apply_learned(s, lens, params) if s.source_class == "projected" else s
              for s in scales]
    out: dict[str, Any] = {"photo_id": photo_id, "photo_key": photo_key(photo_id),
                           "sources": [{"basis": s.basis, "source_class": s.source_class,
                                        "plane": s.plane, "mm_per_px": round(s.mm_per_px, 6),
                                        "rel_sigma": round(s.rel_sigma, 4)} for s in scales],
                           "edge_sigma_px": round(edge_sigma_px, 6), "edge_sigma_source": edge_src}
    anchored = [s for s in scales if s.source_class != "projected"]
    if anchored:
        f = pm.fuse(anchored)
        out["fused"] = {"mm_per_px": round(f["scale"].mm_per_px, 6),
                        "rel_sigma": round(f["scale"].rel_sigma, 4),
                        "chi2_red": f["chi2_red"], "outliers": f["outliers"],
                        "planck_floor_mm": round(pm.planck_floor(f["scale"], edge_sigma_px).mm, 6)}
    if solved and solved["scale_ln"] is not None:
        # solver scale is inches per pixel
        out["solver_mm_per_px"] = round(math.exp(solved["scale_ln"]) * MM_PER_IN, 6)
    return out


def export_error_stats(path: str | Path) -> list[dict[str, Any]]:
    """Unit-free raw-EXIF-vs-anchored residuals by lens → JSON for
    ``Metrology/metrology_cli.py params`` and Perceptopoly. Only (estimator,
    lens, n, mean, sd) leaves — no photo, person, location or part
    identifiers."""
    stats = _local_error_rows(stats_only=True)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(stats, indent=1) + "\n", encoding="utf-8")
    return stats
