"""Label grammar, coordinate assignment, solved-dim override, travel costs."""
from __future__ import annotations

import pytest

from stockopoly import dims, locations, settings


def test_parse_label_variants():
    assert locations.parse_label("A-01-2-B") == {
        "aisle": "A", "bay": 1, "level": 2, "bin": "B"}
    assert locations.parse_label("b012") == {
        "aisle": "B", "bay": 1, "level": 2, "bin": None}
    assert locations.parse_label("AA-103-1") == {
        "aisle": "AA", "bay": 103, "level": 1, "bin": None}
    assert locations.parse_label("DOCK") is None
    assert locations.parse_label("") is None


def test_register_and_coordinates():
    res = locations.register_locations(
        ["A-01-1-A", "A-01-1-B", "A-02-1", "B-01-1", "B-01-2", "garbage!!"])
    assert res["added"] == 5
    assert res["failed"] == ["GARBAGE!!"]

    out = locations.compute_coordinates()
    assert out["located"] == 5 and out["aisles"] == 2

    locs = {l["location_code"]: l for l in locations.list_locations()}
    a1a, a1b = locs["A-01-1-A"], locs["A-01-1-B"]
    b11, b12 = locs["B-01-1"], locs["B-01-2"]

    # aisles separate along x; default pitch = 2*42 + 120
    assert a1a["x"] == 0.0 and b11["x"] == pytest.approx(204.0)
    # two bins split the 96" bay: 48" wide each, centred at 24/72
    assert a1a["w"] == pytest.approx(48.0)
    assert a1a["y"] == pytest.approx(24.0) and a1b["y"] == pytest.approx(72.0)
    # levels stack along z at 60"
    assert b11["z"] == 0.0 and b12["z"] == pytest.approx(60.0)
    assert a1a["capacity_volume"] == pytest.approx(48 * 42 * 60)


def test_solved_dims_shape_the_map():
    # The solver knows the real bay width (120") with high confidence.
    eid = dims.create_entity("rack_member", "bay_width")
    var = dims.ensure_variable(eid, "W")
    dims.add_measurement("absolute", a_var=var, value=120.0)
    dims.solve_all()

    locations.register_locations(["A-01-1", "A-02-1"])
    out = locations.compute_coordinates()
    assert out["bay_width_in"] == pytest.approx(120.0, rel=1e-3)
    locs = {l["location_code"]: l for l in locations.list_locations()}
    # bay 2 spans 120–240, single bin centres at 180
    assert locs["A-02-1"]["y"] == pytest.approx(180.0, rel=1e-3)


def test_travel_costs_monotonic_and_lift_penalty():
    locations.register_locations(["A-01-1", "A-05-1", "A-01-3", "B-01-1"])
    locations.compute_coordinates()
    costs = locations.travel_costs()
    assert set(costs) == {"A-01-1", "A-05-1", "A-01-3", "B-01-1"}
    # farther down the aisle costs more; higher level adds lift penalty
    assert costs["A-05-1"] > costs["A-01-1"]
    assert costs["A-01-3"] == pytest.approx(costs["A-01-1"] + 60.0)
    # second aisle adds cross-aisle distance
    assert costs["B-01-1"] > costs["A-01-1"]


def test_blocked_locations_excluded_and_dock_move():
    locations.register_locations(["A-01-1", "A-02-1"])
    locations.compute_coordinates()
    from stockopoly.store import open_conn
    cn = open_conn()
    try:
        cn.execute("UPDATE locations SET status='blocked' WHERE location_code='A-02-1'")
        cn.commit()
    finally:
        cn.close()
    costs = locations.travel_costs()
    assert "A-02-1" not in costs

    before = costs["A-01-1"]
    locations.set_dock(5000.0, 0.0)
    after = locations.travel_costs()["A-01-1"]
    assert after > before


def test_custom_grammar_setting():
    settings.put("location_grammar",
                 r"^(?P<aisle>Z)(?P<bay>\d{2})(?P<level>\d)$")
    assert locations.parse_label("Z051") == {
        "aisle": "Z", "bay": 5, "level": 1, "bin": None}
    assert locations.parse_label("A-01-1") is None
    res = locations.register_locations(["Z051", "A-01-1"])
    assert res["added"] == 1 and res["failed"] == ["A-01-1"]
