"""Gate for the relational dimension solver (pure math + DB round-trip)."""
from __future__ import annotations

import pytest

from stockopoly import dims
from stockopoly.dims.solver import Measurement, solve
from stockopoly.store import open_conn


# ───────────────────────────────────────────────────────── pure solver layer
def test_single_absolute():
    r = solve(["a"], [], [Measurement("absolute", "a", 96.0, sigma_ln=0.02)])
    assert r.values["a"] == pytest.approx(96.0, rel=1e-6)
    assert r.grounded["a"] is True
    assert r.sigmas_ln["a"] == pytest.approx(0.02, rel=0.05)


def test_pixel_chain_through_photo_scale():
    # 12" ruler spans 600 px in photo p1 → scale 0.02 in/px;
    # box spans 300 px in the same photo → box = 6".
    ms = [
        Measurement("absolute", "ruler", 12.0, sigma_ln=0.01),
        Measurement("pixel_extent", "ruler", 600.0, photo="p1", sigma_ln=0.02),
        Measurement("pixel_extent", "box", 300.0, photo="p1", sigma_ln=0.02),
    ]
    r = solve(["ruler", "box"], ["p1"], ms)
    assert r.scales["p1"] == pytest.approx(0.02, rel=1e-3)
    assert r.values["box"] == pytest.approx(6.0, rel=1e-3)
    assert r.grounded["box"] is True


def test_ratio_identity_pattern_propagation():
    ms = [
        Measurement("absolute", "box_w", 24.0, sigma_ln=0.01),
        Measurement("pattern_count", "bay_w", 4.0, b="box_w", sigma_ln=0.03),
        Measurement("identity", "bay_w2", 0.0, b="bay_w", sigma_ln=0.005),
        Measurement("ratio", "door_w", 0.5, b="bay_w2", sigma_ln=0.05),
    ]
    r = solve(["box_w", "bay_w", "bay_w2", "door_w"], [], ms)
    assert r.values["bay_w"] == pytest.approx(96.0, rel=1e-2)
    assert r.values["bay_w2"] == pytest.approx(96.0, rel=1e-2)
    assert r.values["door_w"] == pytest.approx(48.0, rel=2e-2)


def test_sum_parts_solves_missing_member():
    # bay = s1 + s2 + s3; bay=96, s1=s2=30 → s3 must converge to 36.
    ms = [
        Measurement("absolute", "bay", 96.0, sigma_ln=0.005),
        Measurement("absolute", "s1", 30.0, sigma_ln=0.005),
        Measurement("absolute", "s2", 30.0, sigma_ln=0.005),
        Measurement("sum_parts", "bay", 0.0, parts=["s1", "s2", "s3"],
                    sigma_abs=0.5),
        # weak hint so s3 isn't free-floating at start
        Measurement("ratio", "s3", 1.0, b="s1", sigma_ln=0.5),
    ]
    r = solve(["bay", "s1", "s2", "s3"], [], ms)
    assert r.values["s3"] == pytest.approx(36.0, rel=0.03)
    assert r.grounded["s3"] is True


def test_huber_downweights_outlier():
    ms = [Measurement("absolute", "a", 40.0, sigma_ln=0.02, meas_id=i)
          for i in range(5)]
    ms.append(Measurement("absolute", "a", 80.0, sigma_ln=0.02, meas_id=99))
    r = solve(["a"], [], ms)
    assert r.values["a"] == pytest.approx(40.0, rel=0.05)
    assert 99 in r.outliers


def test_ungrounded_component_detection():
    ms = [
        Measurement("ratio", "x", 2.0, b="y", sigma_ln=0.02),
        Measurement("absolute", "z", 10.0, sigma_ln=0.02),
    ]
    r = solve(["x", "y", "z"], [], ms)
    assert r.grounded["z"] is True
    assert r.grounded["x"] is False and r.grounded["y"] is False
    ung = [c for c in r.components if not c["grounded"]]
    assert len(ung) == 1 and set(ung[0]["members"]) == {"x", "y"}
    # shape is still solved under the gauge
    assert r.values["x"] / r.values["y"] == pytest.approx(2.0, rel=1e-3)


def test_disconnected_var_does_not_break_solve():
    r = solve(["a", "lonely"], [], [Measurement("absolute", "a", 5.0)])
    assert r.values["a"] == pytest.approx(5.0, rel=1e-3)
    assert r.grounded["lonely"] is False


# ─────────────────────────────────────────────────────────────── DB layer
def _mk_entity_var(kind="object", label="Box", axis="W"):
    eid = dims.create_entity(kind, label)
    return eid, dims.ensure_variable(eid, axis)


def test_units_convert_to_inches():
    _, var = _mk_entity_var(label="Rack", axis="H")
    dims.add_measurement("absolute", a_var=var, value=8.0, unit="ft")
    report = dims.solve_all()
    assert report["measurements"] == 1
    e = dims.list_entities()[0]
    assert e["variables"][0]["value"] == pytest.approx(96.0, rel=1e-3)
    assert e["variables"][0]["confidence"] > 0.9


def test_ensure_variable_idempotent():
    eid, var1 = _mk_entity_var()
    assert dims.ensure_variable(eid, "W") == var1
    with pytest.raises(ValueError):
        dims.ensure_variable(eid, "Q")


def test_reference_grounds_photo_and_neighbor(tmp_path):
    # A known 12" reference seen at 600 px grounds the photo; an unknown box
    # at 300 px in the same photo solves to 6".
    ref_id = dims.add_reference_object("Steel ruler", "ruler", dim_l=12.0,
                                       sigma_pct=0.5)
    applied = dims.apply_reference(ref_id, photo_id="ph-1",
                                   pixel_extents={"L": 600})
    assert len(applied["measurements"]) == 2

    _, box_var = _mk_entity_var(label="Mystery box", axis="L")
    dims.add_measurement("pixel_extent", a_var=box_var, value=300,
                         photo_id="ph-1", source="manual")
    report = dims.solve_all()
    assert report["ungrounded_components"] == 0

    detail = dims.entity_detail(2)  # entity 1 = ref, 2 = box
    val = detail["variables"][0]
    assert val["value"] == pytest.approx(6.0, rel=1e-2)
    assert val["ci_low"] < 6.0 < val["ci_high"]

    cn = open_conn()
    try:
        scale = cn.execute("SELECT * FROM photo_scales WHERE photo_id='ph-1'").fetchone()
    finally:
        cn.close()
    assert scale is not None
    import math
    assert math.exp(scale["scale_ln"]) == pytest.approx(0.02, rel=1e-2)


def test_solve_all_flags_outliers_and_reports_ungrounded():
    _, var = _mk_entity_var(label="Crate", axis="W")
    for _ in range(4):
        dims.add_measurement("absolute", a_var=var, value=40.0)
    bad = dims.add_measurement("absolute", a_var=var, value=90.0)

    eid2, var2 = _mk_entity_var(label="Floaty", axis="L")
    _, var3 = _mk_entity_var(label="Floaty2", axis="L")
    dims.add_measurement("ratio", a_var=var2, b_var=var3, value=3.0)

    report = dims.solve_all()
    assert bad in report["outliers"]
    assert report["ungrounded_components"] == 1
    ung = [c for c in report["components"] if not c["grounded"]][0]
    assert "Floaty.L" in ung["members"]

    cn = open_conn()
    try:
        flagged = cn.execute(
            "SELECT meas_id FROM dim_measurements WHERE outlier=1").fetchall()
        ung_conf = cn.execute(
            "SELECT confidence FROM dim_variables WHERE var_id=?", (var2,)).fetchone()
        evts = [r["kind"] for r in cn.execute("SELECT kind FROM events")]
    finally:
        cn.close()
    assert [r["meas_id"] for r in flagged] == [bad]
    assert ung_conf["confidence"] == 0.0
    assert "stockopoly_dims_solved" in evts


def test_measurement_validation():
    _, var = _mk_entity_var()
    with pytest.raises(ValueError):
        dims.add_measurement("ratio", a_var=var, value=2.0)  # missing b_var
    with pytest.raises(ValueError):
        dims.add_measurement("pixel_extent", a_var=var, value=100)  # no photo
    with pytest.raises(ValueError):
        dims.add_measurement("absolute", a_var=var, value=10, unit="furlong")
    with pytest.raises(KeyError):
        dims.add_measurement("absolute", a_var=99999, value=10)
