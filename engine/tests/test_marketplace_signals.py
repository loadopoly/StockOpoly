"""Tests for Ecosystem Marketplace Signals Engine (4 Streams & Symbiotic Feedback)."""

from unittest.mock import MagicMock, patch

import pytest
from stockopoly import marketplace_signals, symbiosis
from stockopoly.marketplace_signals import (
    EcosystemMarketplaceSignalsEngine,
    GEMaintenanceSignal,
    GeographOcrCorpusSignal,
    HubExistentialRequirementSignal,
    MoltbotPurchaseSignal,
    get_marketplace_signals_engine,
)
from stockopoly.store import open_conn


def test_moltbot_purchase_stream():
    engine = EcosystemMarketplaceSignalsEngine()

    with patch("stockopoly.scb_link.log_learning") as mock_scb:
        signal = engine.record_user_moltbot_purchase(
            user_id="user-42",
            moltbot_id="moltbot:driver:01",
            item_id=10887,
            item_name="Grounding Anchor Fixture",
            amount=2,
            pay_with_asset="PTO-ANCHOR-SHARD",
            shards_paid=50,
            world="world1",
        )

        assert isinstance(signal, MoltbotPurchaseSignal)
        assert signal.user_id == "user-42"
        assert signal.moltbot_id == "moltbot:driver:01"
        assert signal.item_id == 10887
        assert signal.shards_paid == 50
        assert signal.serendipity_registered is True

        # Check SCB learning emission
        assert mock_scb.called
        call_args = mock_scb.call_args[0]
        assert call_args[0] == "marketplace_user_moltbot_purchase"
        assert "user-42" in call_args[1]
        assert "moltbot:driver:01" in call_args[1]

    # Verify status reflects moltbot stream
    status = engine.status()
    assert status["counts"]["user_moltbot"] == 1
    assert status["streams"]["user_moltbot"]["count"] == 1
    assert status["scores"]["moltbot_score"] > 0.0


def test_ge_maintenance_stream():
    engine = EcosystemMarketplaceSignalsEngine()

    with patch("stockopoly.scb_link.log_learning") as mock_scb:
        signal = engine.record_ge_maintenance(
            world="world1",
            active_offers=15,
            trades_cleared=8,
            volume_gp=1200.0,
            treasury_gp=24.0,
            bid_ask_spread=0.03,
            npc_participation_ratio=0.65,
        )

        assert isinstance(signal, GEMaintenanceSignal)
        assert signal.world == "world1"
        assert signal.active_offers == 15
        assert signal.trades_cleared == 8
        assert signal.volume_gp == 1200.0
        assert signal.treasury_gp == 24.0
        assert signal.npc_participation_ratio == 0.65

        assert mock_scb.called
        call_args = mock_scb.call_args[0]
        assert call_args[0] == "marketplace_ge_maintenance"

    status = engine.status()
    assert status["counts"]["ge_maintenance"] == 1
    assert status["streams"]["ge_maintenance"]["count"] == 1
    assert status["scores"]["ge_score"] > 0.0


def test_hub_existential_requirement_stream():
    engine = EcosystemMarketplaceSignalsEngine()

    # Pre-seed some inventory
    cn = open_conn()
    try:
        cn.execute(
            "INSERT OR REPLACE INTO inventory (part_number, location_code, qty_oh, as_of) VALUES (?, ?, ?, datetime('now'))",
            ("PN-METAL-STRUT", "A-01-1-A", 1.0),
        )
        cn.commit()
    finally:
        cn.close()

    bom = [
        {"part_number": "PN-METAL-STRUT", "qty": 10.0},
        {"part_number": "mesh/touch/slab-001", "qty": 5.0},
    ]

    with patch("stockopoly.scb_link.emit_body_directive") as mock_dir, patch(
        "stockopoly.scb_link.log_learning"
    ) as mock_scb:
        signal = engine.record_hub_existential_requirement(
            hub_order_id="order-mfg-999",
            recipe_name="anchor",
            bom_requirements=bom,
            target_location="HUB-ASSEMBLY",
            finished_good_asset="PTO-FINISHED-ANCHOR",
        )

        assert isinstance(signal, HubExistentialRequirementSignal)
        assert signal.hub_order_id == "order-mfg-999"
        assert signal.recipe_name == "anchor"
        assert len(signal.physical_shortfalls) == 2  # PN-METAL-STRUT has 1.0 (need 10.0), slab-001 has 0.0
        assert signal.po_generated is True
        assert signal.finished_good_asset == "PTO-FINISHED-ANCHOR"

        # Check directive emitted to Buyer
        assert mock_dir.called
        assert mock_scb.called

    status = engine.status()
    assert status["counts"]["hub_requirements"] == 1
    assert status["streams"]["hub_existential_requirements"]["count"] == 1
    assert status["scores"]["hub_score"] > 0.0


def test_geograph_ocr_corpus_stream():
    engine = EcosystemMarketplaceSignalsEngine()

    with patch("stockopoly.scb_link.log_learning") as mock_scb:
        signal = engine.record_geograph_ocr_corpus_requirement(
            requirement_id="req-geo-test-01",
            target_location="RACK-BAY-B02",
            knowledge_type="pinhole_haversine_relational_anchor",
            non_profit_entity="historical_corpus_society",
            photos_count=4,
            precision_score=0.98,
            corpus_nodes_added=3,
            shards_rewarded=15,
        )

        assert isinstance(signal, GeographOcrCorpusSignal)
        assert signal.requirement_id == "req-geo-test-01"
        assert signal.target_location == "RACK-BAY-B02"
        assert signal.non_profit_entity == "historical_corpus_society"
        assert signal.photos_count == 4
        assert signal.shards_rewarded == 15
        assert signal.corpus_nodes_added == 3

        assert mock_scb.called
        call_args = mock_scb.call_args[0]
        assert call_args[0] == "marketplace_geograph_ocr_corpus_expansion"

    status = engine.status()
    assert status["counts"]["geograph_ocr"] == 1
    assert status["streams"]["geograph_ocr_corpus"]["count"] == 1
    assert status["scores"]["geo_score"] > 0.0


def test_macro_marketplace_index_and_symbiosis_feedback():
    engine = EcosystemMarketplaceSignalsEngine()

    # Add signals to all 4 streams
    engine.record_user_moltbot_purchase("u1", "moltbot:01", 1, "item", 1, "asset", 10)
    engine.record_ge_maintenance("world1", 10, 5, 500.0, 10.0, 0.05, 0.5)
    engine.record_hub_existential_requirement("ord-1", "anchor", [], "LOC")
    engine.record_geograph_ocr_corpus_requirement("req-1", "LOC", "relational", "npo", 2, 0.95, 2, 10)

    status = engine.status()
    macro_idx = status["macro_marketplace_index"]
    assert 0.0 < macro_idx <= 1.0

    # Verify integration with symbiosis orchestrator
    orchestrator = symbiosis.EcosystemSymbiosisOrchestrator()
    orchestrator.signals_engine = engine
    symb_status = orchestrator.status()

    assert "marketplace_signals" in symb_status
    assert symb_status["marketplace_signals"]["macro_marketplace_index"] == macro_idx
    assert symb_status["current_sigma_symbiosis"] >= 0.0
