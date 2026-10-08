"""Tests for Ecosystem Symbiosis, AGV Fleet Planning, and Closed-Loop Manufacturing."""

import json
from unittest.mock import MagicMock, patch

import pytest
from stockopoly import agv, symbiosis
from stockopoly.agv import AGVFleetManager, Waypoint, get_agv_fleet
from stockopoly.store import open_conn
from stockopoly.symbiosis import EcosystemSymbiosisOrchestrator, get_symbiosis_orchestrator


def test_agv_3d_waypoint_planning():
    fleet = AGVFleetManager()

    # Register a location with 3D coordinates (x, y, z)
    cn = open_conn()
    try:
        cn.execute(
            "INSERT OR REPLACE INTO locations (location_code, x, y, z) VALUES (?, ?, ?, ?)",
            ("BIN-RACK-3D", 120.0, 180.0, 64.0),
        )
        cn.commit()
    finally:
        cn.close()

    # Plan path from 3D rack bin to HUB-ASSEMBLY
    waypoints, dist = fleet.plan_path("BIN-RACK-3D", "HUB-ASSEMBLY")

    assert len(waypoints) >= 4
    assert waypoints[0].label.startswith("Pick at")
    assert any("Lower mast" in w.label for w in waypoints)
    assert any("Aisle exit" in w.label for w in waypoints)
    assert any("Cross-way" in w.label for w in waypoints)
    assert waypoints[-1].label.startswith("Deposit at")
    assert dist > 100.0

    # Test status
    status = fleet.status()
    assert "missions_count" in status
    assert "dispatched" in status
    assert "completed" in status


def test_agv_mission_dispatch_and_completion():
    fleet = AGVFleetManager()

    # Ensure source inventory exists for testing
    cn = open_conn()
    try:
        cn.execute(
            "INSERT OR REPLACE INTO inventory (part_number, location_code, qty_oh, as_of) VALUES (?, ?, ?, datetime('now'))",
            ("TEST-PART-01", "A-01-1-A", 10.0),
        )
        cn.commit()
    finally:
        cn.close()

    mission = fleet.dispatch_material_mission(
        part_number="TEST-PART-01",
        qty=3.0,
        from_location="A-01-1-A",
        to_location="HUB-ASSEMBLY",
        demand_id="demand-test-01",
    )

    assert mission.status == "dispatched"
    assert mission.qty == 3.0
    assert len(mission.waypoints) >= 3
    assert mission.travel_distance_in > 0.0

    # Complete mission
    res = fleet.complete_mission(mission.mission_id)
    assert res["ok"] is True
    assert res["mission"]["status"] == "completed"

    # Verify inventory was transferred: source decreased from 10 to 7, destination has 3
    cn = open_conn()
    try:
        src_row = cn.execute(
            "SELECT qty_oh FROM inventory WHERE part_number=? AND location_code=?",
            ("TEST-PART-01", "A-01-1-A"),
        ).fetchone()
        dst_row = cn.execute(
            "SELECT qty_oh FROM inventory WHERE part_number=? AND location_code=?",
            ("TEST-PART-01", "HUB-ASSEMBLY"),
        ).fetchone()

        assert src_row is not None and float(src_row["qty_oh"]) == 7.0
        assert dst_row is not None and float(dst_row["qty_oh"]) >= 3.0
    finally:
        cn.close()


def test_symbiosis_svarog_acre_request():
    orchestrator = EcosystemSymbiosisOrchestrator()

    # Mock external bridges to test complete flow without requiring live HTTP servers
    with patch("stockopoly.hub_link.HubConnectionManager.parse_hub_spatial_ground_truth") as mock_hub_sync, \
         patch("urllib.request.urlopen") as mock_urlopen, \
         patch("stockopoly.dims.wls_engine_bridge._http_post_json") as mock_post_json:

        mock_hub_sync.return_value = {"ok": True, "slabs_read": 7, "updated_count": 6}

        # Mock GARD marketplace HTTP response
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "envelope": {"shard_count": 4, "asset_id": "test-asset"},
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        # Mock QUIPU observation response
        mock_post_json.return_value = (True, {"ok": True, "observed": True})

        report = orchestrator.svarog_acre_request(
            recipe_name="anchor",
            requester="svarog",
            target_location="HUB-ASSEMBLY",
        )

        assert report.status == "COMPLETED"
        assert report.recipe_name == "anchor"
        assert report.requester == "svarog"
        assert len(report.agv_missions) == 2  # PN-METAL-STRUT and mesh/touch/slab-001
        assert report.sigma_symbiosis > 0.0
        assert report.touch_metrology_verified is True
        assert report.gard_shards_emitted == 4

        # Status check
        status = orchestrator.status()
        assert status["total_cycles_completed"] >= 1
        assert status["current_sigma_symbiosis"] > 0.0
