"""Photogrammetric connection: vendored contract, standards, EXIF prior, privacy."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from stockopoly import dims, intake, metrology, settings
from stockopoly import photometrology as pm
from stockopoly.intake.exif import parse_jpeg
from stockopoly.store import open_conn
from tests.helpers import build_exif_tiff, build_jpeg

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "metrology_golden.json"

FINGER = {"schema": pm.SCHEMA, "kind": "local_standard", "owner": "adam",
          "part": "finger.index.middle_phalanx.width", "value_mm": 15.587,
          "sigma_mm": 1.2327, "method": "tape", "calibrated_at": "2026-06-10",
          "valid_until": "2027-06-10", "definition": "right index, middle phalanx",
          "evidence": [{"photo": "x.jpg", "value_mm": 15.587}], "privacy": "personal"}


def test_vendored_module_is_the_canonical_one():
    """Edit Metrology/photometrology.py, then run metrology_cli.py sync."""
    gv = json.loads(FIXTURE.read_text(encoding="utf-8"))
    src = Path(pm.__file__).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(src).hexdigest() == gv["module_sha256"]
    c = gv["cases"]
    e = pm.scale_from_exif(*c["scale_from_exif"]["in"])
    assert e.ln_mm_per_px == pytest.approx(c["scale_from_exif"]["out"]["ln"], abs=1e-12)
    assert pm.photo_key(c["photo_key"]["in"].encode()) == c["photo_key"]["out"]


def _photo(tmp_path, *, focus=(135, 1000), focal_35=24, w=4080, h=3072) -> str:
    src = tmp_path / "shots"
    src.mkdir(exist_ok=True)
    (src / "card.jpg").write_bytes(build_jpeg(w, h, exif=build_exif_tiff(
        focal_35=focal_35, subject_distance=focus)))
    summary = intake.ingest_loose(src)
    return intake.batch_photos(summary["batch_id"])[0]["photo_id"]


def test_exif_reads_subject_distance(tmp_path):
    jpg = tmp_path / "d.jpg"
    jpg.write_bytes(build_jpeg(800, 600, exif=build_exif_tiff(subject_distance=(88, 1000))))
    assert parse_jpeg(jpg)["focus_distance_m"] == pytest.approx(0.088)
    jpg.write_bytes(build_jpeg(800, 600, exif=build_exif_tiff()))
    assert parse_jpeg(jpg)["focus_distance_m"] is None


def test_photo_key_is_content_hash(tmp_path):
    pid = _photo(tmp_path)
    cn = open_conn()
    try:
        path = cn.execute("SELECT abs_path FROM photos WHERE photo_id=?", (pid,)).fetchone()[0]
    finally:
        cn.close()
    assert metrology.photo_key(pid) == pm.photo_key(path)


def test_personal_standard_needs_its_owner(tmp_path):
    pid = _photo(tmp_path)
    key = metrology.register_standard(FINGER)
    with pytest.raises(PermissionError):
        metrology.apply_standard(key, pid, 400.0, photographed_owner="someone_else")
    with pytest.raises(PermissionError):
        metrology.apply_standard(key, pid, 400.0)


def test_standard_grounds_solver_photo_scale(tmp_path):
    pid = _photo(tmp_path)
    key = metrology.register_standard(FINGER)
    out = metrology.apply_standard(key, pid, 400.0, pixel_sigma=4.0,
                                   photographed_owner="adam")
    assert out["mm_per_px"] == pytest.approx(15.587 / 400.0, rel=5e-5)  # rounded to 6 dp
    report = dims.solve_all()
    assert report["ungrounded_components"] == 0
    rep = metrology.photo_report(pid)
    # solver (inches internally) and metrology agree once converted
    assert rep["solver_mm_per_px"] == pytest.approx(15.587 / 400.0, rel=1e-3)
    assert rep["fused"]["mm_per_px"] == pytest.approx(15.587 / 400.0, rel=5e-5)
    # smallest resolvable length on that plane: the span was read to ±4 px, so
    # each edge to 4/√2 px and the floor is mm/px · 4 (dynamic, not assumed)
    assert rep["edge_sigma_source"] == "from span reading uncertainty"
    assert rep["fused"]["planck_floor_mm"] == pytest.approx(15.587 / 400.0 * 4.0, rel=1e-3)


def test_exif_prior_logged_but_not_solved_by_default(tmp_path):
    pid = _photo(tmp_path)
    out = metrology.add_exif_prior(pid)
    assert out["in_solver"] is False
    expected = pm.scale_from_exif(4080, 3072, 24, 0.135).mm_per_px
    assert out["mm_per_px"] == pytest.approx(expected, rel=1e-5)
    settings.put("metrology_exif_prior", True)
    out2 = metrology.add_exif_prior(pid)
    assert out2["in_solver"] is True
    assert dims.solve_all()["ungrounded_components"] == 0


def test_public_view_hides_owner_basis_and_personal_pixels(tmp_path):
    pid = _photo(tmp_path)
    key = metrology.register_standard(FINGER)
    metrology.apply_standard(key, pid, 400.0, photographed_owner="adam")
    metrology.apply_reference_key("tcg_standard.width", pid, 1600.0)
    cn = open_conn()
    try:
        rows = [dict(r) for r in cn.execute("SELECT * FROM metrology_obs_public ORDER BY local_id")]
    finally:
        cn.close()
    assert {"owner", "basis"} & set(rows[0]) == set()
    std_row, ref_row = rows
    assert std_row["ref_kind"] == "standard" and std_row["pixels"] is None
    # the scale alone does not reveal the finger: pixels withheld
    assert ref_row["ref_kind"] == "reference" and ref_row["pixels"] == 1600.0
    assert all(r["photo_key"].startswith("sha256:") for r in rows)
    assert all(r["source_app"] == "stockopoly" for r in rows)


def test_export_error_stats_is_unit_free(tmp_path):
    pid = _photo(tmp_path)
    metrology.add_exif_prior(pid)
    metrology.apply_reference_key("tcg_standard.width", pid, 1311.0)
    out = tmp_path / "stats.json"
    stats = metrology.export_error_stats(out)
    assert len(stats) == 1 and stats[0]["lens"] == "24" and stats[0]["n"] == 1
    text = out.read_text(encoding="utf-8")
    assert pid not in text and "sha256" not in text and "adam" not in text
    exif = pm.scale_from_exif(4080, 3072, 24, 0.135).mm_per_px
    assert stats[0]["mean_log_err"] == pytest.approx(math.log(exif / (63.0 / 1311.0)), abs=1e-5)


def test_supabase_mirror_includes_public_metrology_view():
    from stockopoly import supabase_sync
    remote, mode, col, conflict = supabase_sync._TABLES["metrology_obs_public"]
    assert remote == "metrology_observations" and conflict == "source_app,local_id"
    assert "metrology_standards" not in supabase_sync._TABLES


# ───────────────────────────── dynamic parameters
def _anchored_photo(tmp_path, name, *, focus, true_mm_per_px, monkeypatch=None):
    src = tmp_path / name
    src.mkdir()
    (src / f"{name}.jpg").write_bytes(build_jpeg(4080, 3072, exif=build_exif_tiff(
        focal_35=24, subject_distance=focus), filler=name.encode()))
    pid = intake.batch_photos(intake.ingest_loose(src)["batch_id"])[0]["photo_id"]
    metrology.add_exif_prior(pid)
    metrology.apply_reference_key("tcg_standard.width", pid, 63.0 / true_mm_per_px, pixel_sigma=1.0)
    return pid


def test_exif_bias_and_sigma_are_learned_from_anchored_photos(tmp_path, monkeypatch):
    monkeypatch.delenv("HOLON_METROLOGY_PARAMS", raising=False)
    raw = pm.scale_from_exif(4080, 3072, 24, 0.135).mm_per_px
    # field truth: this phone's EXIF reads 30 % long
    for i in range(12):
        _anchored_photo(tmp_path, f"a{i}", focus=(135, 1000), true_mm_per_px=raw / 1.3)
    p = metrology.learned_params()
    assert p["exif"]["24"]["n"] == 12
    assert p["exif"]["24"]["bias_ln"] == pytest.approx(12 * math.log(1.3) / 15, rel=1e-3)
    assert p["exif"]["24"]["sigma_ln"] < pm.EXIF_SIGMA_LN
    # a new photo: the prior it contributes is now de-biased and tighter
    fresh = _anchored_photo(tmp_path, "new", focus=(135, 1000), true_mm_per_px=raw / 1.3)
    s = metrology.exif_scale(fresh)
    assert s.basis == "exif:debiased" and s.mm_per_px < raw
    assert metrology.exif_scale(fresh, learned=False).mm_per_px == pytest.approx(raw)
    # the stored observation stays raw, so the statistics keep measuring the camera
    cn = open_conn()
    try:
        ln = cn.execute("SELECT ln_mm_per_px FROM metrology_observations WHERE photo_id=?"
                        " AND ref_kind='exif'", (fresh,)).fetchone()[0]
    finally:
        cn.close()
    assert math.exp(ln) == pytest.approx(raw)


def test_shared_params_file_takes_over(tmp_path, monkeypatch):
    f = tmp_path / "params.json"
    f.write_text(json.dumps(pm.params_from_stats(
        [{"kind": "error_stat", "estimator": "exif", "lens": "24", "n": 500,
          "mean_log_err": -0.1, "sd_log_err": 0.05}], grid_fit={"k": 2.5, "n": 1000})))
    monkeypatch.setenv("HOLON_METROLOGY_PARAMS", str(f))
    p = metrology.learned_params()
    assert p["exif"]["24"]["n"] == 500 and pm.grid_k(p) == pytest.approx(2.5, rel=0.01)


def test_report_edge_sigma_is_dynamic(tmp_path, monkeypatch):
    monkeypatch.delenv("HOLON_METROLOGY_PARAMS", raising=False)
    pid = _photo(tmp_path)
    metrology.apply_reference_key("tcg_standard.width", pid, 1400.0, pixel_sigma=3.0)
    rep = metrology.photo_report(pid)
    assert rep["edge_sigma_source"] == "from span reading uncertainty"
    assert rep["edge_sigma_px"] == pytest.approx(3.0 / math.sqrt(2), abs=1e-4)
    assert rep["fused"]["planck_floor_mm"] == pytest.approx(63 / 1400 * 3.0, rel=1e-3)


def test_measure_edge_from_real_pixels(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    import random
    rng = random.Random(3)
    img = PIL.new("L", (200, 120))
    px = img.load()
    for y in range(120):
        for x in range(200):
            px[x, y] = max(0, min(255, int(40 + 150 / (1 + math.exp(-(x - 100) / 1.5))
                                           + rng.gauss(0, 3))))
    src = tmp_path / "edge"
    src.mkdir()
    img.save(src / "edge.png")
    img.convert("RGB").save(src / "edge.jpg", quality=95)
    (src / "edge.png").unlink()
    pid = intake.batch_photos(intake.ingest_loose(src)["batch_id"])[0]["photo_id"]
    res = metrology.measure_edge(pid, (80, 10, 120, 110), axis="vertical")
    assert 0 < res["edge_sigma_px"] < 0.5 and res["contrast"] > 100
    metrology.apply_reference_key("tcg_standard.width", pid, 150.0, pixel_sigma=5.0)
    rep = metrology.photo_report(pid)
    assert rep["edge_sigma_source"].startswith("measured")
    assert rep["edge_sigma_px"] == pytest.approx(res["edge_sigma_px"], abs=1e-6)  # 6 dp


def test_grid_uses_learned_constant(tmp_path, monkeypatch):
    pid = _photo(tmp_path)
    idx = [(i, j) for i in range(5) for j in range(4)]
    pts = [(300 + 40.0 * i, 200 + 40.0 * j) for i, j in idx]
    monkeypatch.delenv("HOLON_METROLOGY_PARAMS", raising=False)
    a = metrology.apply_grid(pid, pts, idx, "pegboard.pitch_1in")
    assert a["mm_per_px"] == pytest.approx(25.4 / 40, rel=1e-6) and a["grid_k"] == pm.GRID_SIGMA_K
    f = tmp_path / "p.json"
    f.write_text(json.dumps(pm.params_from_stats([], grid_fit={"k": 1.0, "n": 5000})))
    monkeypatch.setenv("HOLON_METROLOGY_PARAMS", str(f))
    b = metrology.apply_grid(pid, pts, idx, "pegboard.pitch_1in")
    assert b["grid_k"] == pytest.approx(1.0, rel=0.01)
