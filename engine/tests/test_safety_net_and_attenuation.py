"""Tests for Separable Safety Net, Marketplace Attenuation, and Hub Link."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from stockopoly import dims, hub_link, scb_link
from stockopoly.dims.marketplace_attenuation import (
    MarketplaceAttenuationEngine,
    get_attenuation_engine,
)
from stockopoly.dims.quipu_safety_net import QuipuSafetyNet, get_safety_net
from stockopoly.slotting import migration


def test_quipu_safety_net_direct_success(tmp_path):
    spool = tmp_path / "safety_spool.jsonl"
    net = QuipuSafetyNet(spool_path=spool, fast_timeout=1.0, fail_threshold=2)

    def mock_dispatcher(url, payload, timeout):
        return True, {"ok": True, "mesh": {"vocab": 100}}

    payload = {
        "source": "perceptopoly",
        "meta": {"relational": {"readout": {"r": 0.5, "control": 0.9}}},
    }
    res = net.execute("http://fake-quipu/observe", payload, mock_dispatcher)
    assert res["ok"] is True
    assert res["safety_net"] == "direct_pass"
    assert net.status()["circuit_state"] == "CLOSED"
    assert net.status()["spool_depth"] == 0


def test_quipu_safety_net_trips_to_open_and_spools(tmp_path):
    spool = tmp_path / "safety_spool.jsonl"
    net = QuipuSafetyNet(spool_path=spool, fast_timeout=0.1, fail_threshold=2)

    def failing_dispatcher(url, payload, timeout):
        return False, "Connection refused"

    # Call 1: fails, spools, failures=1
    p1 = {"meta": {"relational": {"readout": {"r": 0.1, "control": 0.5}}}}
    res1 = net.execute("http://fake-quipu/observe", p1, failing_dispatcher)
    assert res1["ok"] is True
    assert res1["spooled"] is True
    assert net.status()["circuit_state"] == "CLOSED"

    # Call 2: fails, spools, failures=2 -> TRIPS TO OPEN
    p2 = {"meta": {"relational": {"readout": {"r": 0.2, "control": 0.4}}}}
    res2 = net.execute("http://fake-quipu/observe", p2, failing_dispatcher)
    assert res2["ok"] is True
    assert res2["spooled"] is True
    assert net.status()["circuit_state"] == "OPEN"

    # Call 3: circuit is OPEN -> diverts immediately to spool without even calling dispatcher!
    called = []

    def probe_dispatcher(url, payload, timeout):
        called.append(True)
        return True, {"ok": True}

    p3 = {"meta": {"relational": {"readout": {"r": 0.3, "control": 0.3}}}}
    res3 = net.execute("http://fake-quipu/observe", p3, probe_dispatcher)
    assert res3["ok"] is True
    assert res3["safety_net"] == "diverted_open_circuit"
    assert len(called) == 0  # Did not attempt dispatcher
    assert net.status()["spool_depth"] == 3


def test_quipu_safety_net_planck_gate(tmp_path):
    spool = tmp_path / "safety_spool.jsonl"
    net = QuipuSafetyNet(spool_path=spool)

    def mock_dispatcher(url, payload, timeout):
        return True, {"ok": True}

    # First call: initial state
    p1 = {"meta": {"relational": {"readout": {"r": 0.5000, "control": 0.8000}}}}
    r1 = net.execute("http://fake-quipu/observe", p1, mock_dispatcher)
    assert r1["safety_net"] == "direct_pass"

    # Second call: zero displacement (|Δ| < 1e-4) -> Planck gate retains heartbeat
    p2 = {"meta": {"relational": {"readout": {"r": 0.50001, "control": 0.80001}}}}
    r2 = net.execute("http://fake-quipu/observe", p2, mock_dispatcher)
    assert r2["planck_gated"] is True
    assert r2["safety_net"] == "heartbeat_retained"


def test_quipu_safety_net_flush(tmp_path):
    spool = tmp_path / "safety_spool.jsonl"
    net = QuipuSafetyNet(spool_path=spool)
    net.spool("http://fake-quipu/observe", {"test": 1}, reason="test")
    net.spool("http://fake-quipu/observe", {"test": 2}, reason="test")
    assert net.status()["spool_depth"] == 2

    def success_dispatcher(url, payload, timeout):
        return True, {"ok": True}

    flush_res = net.flush(success_dispatcher)
    assert flush_res["ok"] is True
    assert flush_res["drained"] == 2
    assert net.status()["spool_depth"] == 0


def test_marketplace_attenuation_computation():
    engine = MarketplaceAttenuationEngine(lambda_crb=0.50)

    # High precision: CRB = 0.0 -> Alpha = 1.0
    alpha_high = engine.compute_shard_attenuation(crb_mean=0.0, authenticated=True)
    assert alpha_high == 1.0

    # Moderate uncertainty: CRB = 2.0 -> Alpha = 1.0 / (1 + 1.0) = 0.50
    alpha_mid = engine.compute_shard_attenuation(crb_mean=2.0, authenticated=True)
    assert alpha_mid == 0.50

    # Unauthenticated envelope penalty: S_auth = 0.60
    alpha_unauth = engine.compute_shard_attenuation(crb_mean=0.0, authenticated=False)
    assert alpha_unauth == 0.60


def test_marketplace_attenuation_modulates_gauge():
    engine = MarketplaceAttenuationEngine(lambda_crb=0.50)
    engine.record_shard_interaction(
        entity_id=1,
        token_id="DCC1-WLS-1",
        authenticated=True,
        fisher_info_dict={"L": 10.0, "W": 10.0},
        crb_dict={"L": 2.0, "W": 2.0},
        dimensions={"L": 12.0, "W": 8.0},
    )
    # alpha should be 0.50
    assert engine.status()["current_alpha_attenuation"] == 0.50

    # Gauge sigma modulated: base 10.0 * 1.0 * 0.50 = 5.0
    sigma_att = engine.attenuate_gauge_sigma(base_sigma=10.0, w_phase=1.0)
    assert sigma_att == 5.0


def test_hub_link_parses_slabs(monkeypatch):
    mgr = hub_link.HubConnectionManager()

    mock_slabs = {
        "groundTruth": [
            {
                "slabId": "slab-001",
                "label": "Test Acrylic Slab",
                "thicknessMm": 50.8,  # Exactly 2 inches
                "sigmaMm": 0.508,
                "refractiveIndex": 1.49,
                "sigmaPct": 1.0,
                "referenceName": "mesh/touch/slab-001",
            }
        ]
    }

    monkeypatch.setattr(mgr, "resolve_hub_url", lambda: "http://mock-hub:8000")
    monkeypatch.setattr(
        hub_link,
        "_http_get_json",
        lambda url, timeout=4.0, **kwargs: (True, mock_slabs),
    )

    res = mgr.parse_hub_spatial_ground_truth()
    assert res["ok"] is True
    assert res["slabs_read"] == 1

    refs = dims.list_reference_objects()
    matching = [r for r in refs if r["name"] == "mesh/touch/slab-001"]
    assert len(matching) == 1
    assert round(matching[0]["dim_h"], 1) == 2.0


def test_emit_body_directive(fake_scb):
    # fake_scb creates brain_kv, learning_log, and we create body_directives table
    db_file = fake_scb / "pipeline" / "local_brain.sqlite"
    cn = sqlite3.connect(str(db_file))
    cn.execute(
        """
        CREATE TABLE IF NOT EXISTS body_directives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            fingerprint TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            signal_kind TEXT NOT NULL,
            priority REAL NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            why_it_matters TEXT,
            do_this TEXT,
            owner_role TEXT,
            target_entity TEXT,
            evidence_json TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            last_status_at TEXT,
            value_per_year REAL
        )
        """
    )
    cn.commit()
    cn.close()

    ok = scb_link.emit_body_directive(
        title="Test Slotting Rebalance",
        why_it_matters="Travel cost reduction",
        do_this="Move 10 pallets to zone A",
        owner_role="Ops",
        priority=0.9,
        severity="act",
        fingerprint="test:fp:1",
    )
    assert ok is True

    cn = sqlite3.connect(str(db_file))
    row = cn.execute("SELECT title, priority, status FROM body_directives WHERE fingerprint='test:fp:1'").fetchone()
    cn.close()
    assert row is not None
    assert row[0] == "Test Slotting Rebalance"
    assert row[1] == 0.9
    assert row[2] == "open"
