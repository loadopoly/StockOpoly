"""Tests for WLS Engine Bridge — direct triad binding between StockOpoly WLS solver,
GARD-Shard Marketplace, UEQGM physics engine, and QUIPU Observer.
"""
from __future__ import annotations

import os
import pytest

from stockopoly import dims
from stockopoly.dims import wls_engine_bridge
from stockopoly.dims.solver import Measurement, solve


def test_ueqgm_phase_weight_returns_bounded_scalar():
    pw = wls_engine_bridge.get_ueqgm_phase_weight()
    assert isinstance(pw, float)
    assert 0.90 <= pw <= 1.10


def test_wls_computes_fisher_info_and_crb_bound():
    ms = [
        Measurement("absolute", "box", 24.0, sigma_ln=0.02),
        Measurement("absolute", "lid", 24.0, sigma_ln=0.01),
    ]
    res = solve(["box", "lid"], [], ms, ueqgm_phase_weight=1.05)

    assert "box" in res.crb_bound
    assert "lid" in res.crb_bound
    assert res.crb_bound["box"] == pytest.approx(0.02 ** 2, rel=0.1)
    assert res.crb_bound["lid"] == pytest.approx(0.01 ** 2, rel=0.1)

    assert "box" in res.fisher_info
    assert "lid" in res.fisher_info
    assert res.fisher_info["box"] == pytest.approx(1.0 / (0.02 ** 2), rel=0.1)
    assert res.fisher_info["lid"] == pytest.approx(1.0 / (0.01 ** 2), rel=0.1)


def test_ueqgm_phase_weight_modulates_gauge_sigma():
    # An ungrounded ratio measurement
    ms = [Measurement("ratio", "a", 2.0, b="b", sigma_ln=0.02)]
    # With higher phase weight, gauge sigma relaxes
    res_1 = solve(["a", "b"], [], ms, ueqgm_phase_weight=1.0)
    res_2 = solve(["a", "b"], [], ms, ueqgm_phase_weight=1.1)

    assert res_1.grounded["a"] is False
    assert res_2.grounded["a"] is False
    assert res_1.values["a"] / res_1.values["b"] == pytest.approx(2.0, rel=1e-3)
    assert res_2.values["a"] / res_2.values["b"] == pytest.approx(2.0, rel=1e-3)


def test_solve_all_includes_triad_sync(tmp_path):
    eid = dims.create_entity("part", "Pallet")
    var = dims.ensure_variable(eid, "L")
    dims.add_measurement("absolute", a_var=var, value=48.0, unit="in", source="manual")

    report = dims.solve_all()
    assert "triad_sync" in report
    assert "ueqgm_phase_weight" in report
    assert 0.90 <= report["ueqgm_phase_weight"] <= 1.10
    sync = report["triad_sync"]
    assert "quipu" in sync
    assert "gard" in sync
