"""Ecosystem Marketplace Signals Engine — 4-Stream Market Dynamics & Relational Feedback.

Ingests, coordinates, and attenuates four distinct marketplace signal streams:
1. TheSeuScape Marketplace: User purchasing in-game assets/shards for a Moltbot driver,
   delivering programmatic value via serendipity without epistemic world-model contamination.
2. Grand Exchange Maintenance: Agents (quipu_forager, quipu_pioneer, quipu_courier) and
   NPCs (Svarog Forge, Bankers, Host NPCs) interacting on the shared order book to balance
   liquidity, clear trades, manage bid-ask spreads, and pay shared treasury fees.
3. Hub Existential Requirements: HubCore manufacturing work orders, facility operations,
   and build schedules driving BOM part shortages, ERP PO requisitions, and finished-good
   tokenization into the physical existential realm.
4. Geograph OCR Corpus Requirements: Epistemic demand to acquire specific relational
   knowledge (camera pinhole + Haversine raycast fused coordinates, physical condition,
   part numbers, spatial scale) from location/pictures to expand the non-profit shared Corpus.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import events, scb_link
from .dims import marketplace_attenuation, quipu_safety_net, wls_engine_bridge
from .store import open_conn

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MoltbotPurchaseSignal:
    signal_id: str
    user_id: str
    moltbot_id: str
    item_id: int
    item_name: str
    amount: int
    pay_with_asset: str
    shards_paid: int
    world: str = "world1"
    serendipity_registered: bool = True
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GEMaintenanceSignal:
    signal_id: str
    world: str
    active_offers: int
    trades_cleared: int
    volume_gp: float
    treasury_gp: float
    bid_ask_spread: float
    npc_participation_ratio: float
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HubExistentialRequirementSignal:
    signal_id: str
    hub_order_id: str
    recipe_name: str
    bom_requirements: list[dict[str, Any]]
    target_location: str
    physical_shortfalls: list[dict[str, Any]]
    po_generated: bool
    finished_good_asset: Optional[str] = None
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GeographOcrCorpusSignal:
    signal_id: str
    requirement_id: str
    target_location: str
    knowledge_type: str
    non_profit_entity: str
    photos_count: int
    precision_score: float
    corpus_nodes_added: int
    shards_rewarded: int
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EcosystemMarketplaceSignalsEngine:
    """Unifies the 4 marketplace signal streams across TheSeuScape, GE, Hub, and Geograph OCR."""

    def __init__(self):
        self._moltbot_signals: list[MoltbotPurchaseSignal] = []
        self._ge_signals: list[GEMaintenanceSignal] = []
        self._hub_requirements: list[HubExistentialRequirementSignal] = []
        self._geograph_ocr_signals: list[GeographOcrCorpusSignal] = []
        self.attenuation = marketplace_attenuation.get_attenuation_engine()
        self.safety_net = quipu_safety_net.get_safety_net()

    # -------------------------------------------------------------------------
    # Stream 1: User purchasing for a Moltbot in TheSeuScape
    # -------------------------------------------------------------------------
    def record_user_moltbot_purchase(
        self,
        user_id: str,
        moltbot_id: str,
        item_id: int,
        item_name: str,
        amount: int,
        pay_with_asset: str,
        shards_paid: int,
        world: str = "world1",
    ) -> MoltbotPurchaseSignal:
        sig_id = f"sig-molt-{uuid.uuid4().hex[:8]}"
        signal = MoltbotPurchaseSignal(
            signal_id=sig_id,
            user_id=user_id,
            moltbot_id=moltbot_id,
            item_id=item_id,
            item_name=item_name,
            amount=amount,
            pay_with_asset=pay_with_asset,
            shards_paid=shards_paid,
            world=world,
            serendipity_registered=True,
            timestamp=_now(),
        )
        self._moltbot_signals.append(signal)

        # Attenuate shard link for this purchase
        self.attenuation.record_shard_interaction(
            entity_id=item_id,
            token_id=f"PTO-ITEM-{item_id}",
            authenticated=True,
            fisher_info_dict={"L": 500.0, "W": 500.0},
            crb_dict={"L": 0.002, "W": 0.002},
            dimensions={"qty": float(amount), "shards_paid": float(shards_paid)},
        )

        # Emit learning to SCB
        scb_link.log_learning(
            "marketplace_user_moltbot_purchase",
            f"User {user_id} purchased {amount}x {item_name} for Moltbot {moltbot_id} (settled via {shards_paid} shards of {pay_with_asset})",
            signal.to_dict(),
            signal=0.88,
        )

        # Post observation to QUIPU via safety net
        quipu_obs = {
            "source": "perceptopoly",
            "text": f"Moltbot {moltbot_id} gifted {amount}x {item_name} by user {user_id}; queued for serendipity delivery.",
            "meta": {
                "relational": {
                    "user_id": user_id,
                    "moltbot_id": moltbot_id,
                    "item_id": item_id,
                    "shards_paid": shards_paid,
                    "stream": "user_moltbot_marketplace",
                }
            },
        }
        self.safety_net.execute(
            f"{wls_engine_bridge.DEFAULT_QUIPU_HOST}/observe",
            quipu_obs,
            wls_engine_bridge._http_post_json,
        )

        events.record("user_moltbot_purchase", signal.to_dict(), title=f"User purchased for Moltbot {moltbot_id}", signal=0.85)
        return signal

    # -------------------------------------------------------------------------
    # Stream 2: Agents & NPCs interacting to maintain Grand Exchange
    # -------------------------------------------------------------------------
    def record_ge_maintenance(
        self,
        world: str,
        active_offers: int,
        trades_cleared: int,
        volume_gp: float,
        treasury_gp: float,
        bid_ask_spread: float,
        npc_participation_ratio: float,
    ) -> GEMaintenanceSignal:
        sig_id = f"sig-ge-{uuid.uuid4().hex[:8]}"
        signal = GEMaintenanceSignal(
            signal_id=sig_id,
            world=world,
            active_offers=active_offers,
            trades_cleared=trades_cleared,
            volume_gp=volume_gp,
            treasury_gp=treasury_gp,
            bid_ask_spread=bid_ask_spread,
            npc_participation_ratio=npc_participation_ratio,
            timestamp=_now(),
        )
        self._ge_signals.append(signal)

        scb_link.log_learning(
            "marketplace_ge_maintenance",
            f"GE Order Book maintained in {world}: {trades_cleared} trades, {volume_gp:.0f} gp volume, treasury {treasury_gp:.0f} gp (spread={bid_ask_spread:.2f})",
            signal.to_dict(),
            signal=0.82,
        )

        # Feed to QUIPU
        quipu_obs = {
            "source": "perceptopoly",
            "text": f"Grand Exchange liquidity maintenance on {world}: {active_offers} active offers, spread {bid_ask_spread:.2f}, NPC ratio {npc_participation_ratio:.2f}.",
            "meta": {
                "relational": {
                    "world": world,
                    "trades_cleared": trades_cleared,
                    "volume_gp": volume_gp,
                    "treasury_gp": treasury_gp,
                    "stream": "ge_liquidity_maintenance",
                }
            },
        }
        self.safety_net.execute(
            f"{wls_engine_bridge.DEFAULT_QUIPU_HOST}/observe",
            quipu_obs,
            wls_engine_bridge._http_post_json,
        )

        events.record("ge_maintenance_interaction", signal.to_dict(), title=f"GE Maintenance in {world}", signal=0.80)
        return signal

    # -------------------------------------------------------------------------
    # Stream 3: Hub activities driving part/finished good existential requirements
    # -------------------------------------------------------------------------
    def record_hub_existential_requirement(
        self,
        hub_order_id: str,
        recipe_name: str,
        bom_requirements: list[dict[str, Any]],
        target_location: str = "HUB-ASSEMBLY",
        physical_shortfalls: Optional[list[dict[str, Any]]] = None,
        po_generated: bool = False,
        finished_good_asset: Optional[str] = None,
    ) -> HubExistentialRequirementSignal:
        sig_id = f"sig-exist-{uuid.uuid4().hex[:8]}"
        if physical_shortfalls is None and bom_requirements:
            shortfalls = []
            try:
                cn = open_conn()
                try:
                    for item in bom_requirements:
                        pn = item.get("part_number")
                        qty_req = float(item.get("qty", 0.0))
                        row = cn.execute(
                            "SELECT SUM(qty_oh) AS total FROM inventory WHERE part_number=?",
                            (pn,),
                        ).fetchone()
                        oh = float(row["total"]) if (row and row["total"] is not None) else 0.0
                        if oh < qty_req:
                            shortfalls.append({
                                "part_number": pn,
                                "required": qty_req,
                                "on_hand": oh,
                                "shortfall": qty_req - oh,
                            })
                finally:
                    cn.close()
            except Exception as exc:
                logger.debug("Failed to calculate inventory shortfalls: %s", exc)
                shortfalls = []
            po_generated = len(shortfalls) > 0
        else:
            shortfalls = physical_shortfalls or []
        signal = HubExistentialRequirementSignal(
            signal_id=sig_id,
            hub_order_id=hub_order_id,
            recipe_name=recipe_name,
            bom_requirements=bom_requirements,
            target_location=target_location,
            physical_shortfalls=shortfalls,
            po_generated=po_generated,
            finished_good_asset=finished_good_asset,
            timestamp=_now(),
        )
        self._hub_requirements.append(signal)

        # If shortfalls exist, emit actionable directive to SCB body_directives
        if shortfalls:
            scb_link.emit_body_directive(
                title=f"Existential Market Shortfall: Procure {len(shortfalls)} parts for Hub Order {hub_order_id}",
                why_it_matters=f"Manufacturing recipe '{recipe_name}' requires physical parts that are below reorder threshold.",
                do_this="Dispatch PO requisitions to ERP / supplier marketplace and route arriving stock to WIP staging.",
                owner_role="Buyer",
                priority=0.92,
                severity="act",
                fingerprint=f"hub:mfg:shortfall:{hub_order_id}",
                evidence={"shortfalls": shortfalls, "order_id": hub_order_id},
            )

        scb_link.log_learning(
            "marketplace_hub_existential_requirement",
            f"Hub Order {hub_order_id} ({recipe_name}) drove physical market requirements: {len(bom_requirements)} BOM items, {len(shortfalls)} shortfalls",
            signal.to_dict(),
            signal=0.90,
        )

        events.record("hub_existential_requirement", signal.to_dict(), title=f"Hub Requirement {hub_order_id}", signal=0.88)
        return signal

    # -------------------------------------------------------------------------
    # Stream 4: Geograph OCR requirements to acquire relational knowledge for non-profit Corpus
    # -------------------------------------------------------------------------
    def record_geograph_ocr_corpus_requirement(
        self,
        requirement_id: str,
        target_location: str,
        knowledge_type: str,
        non_profit_entity: str,
        photos_count: int = 1,
        precision_score: float = 0.95,
        corpus_nodes_added: int = 1,
        shards_rewarded: int = 5,
    ) -> GeographOcrCorpusSignal:
        sig_id = f"sig-geo-{uuid.uuid4().hex[:8]}"
        signal = GeographOcrCorpusSignal(
            signal_id=sig_id,
            requirement_id=requirement_id,
            target_location=target_location,
            knowledge_type=knowledge_type,
            non_profit_entity=non_profit_entity,
            photos_count=photos_count,
            precision_score=precision_score,
            corpus_nodes_added=corpus_nodes_added,
            shards_rewarded=shards_rewarded,
            timestamp=_now(),
        )
        self._geograph_ocr_signals.append(signal)

        # Tokenize / reward the non-profit contributor in the GARD Marketplace
        try:
            gard_host = os.environ.get("GARD_MARKETPLACE_URL") or "http://loadopoly-gard-marketplace:8600"
            tok_payload = {
                "asset_id": f"geograph-corpus-{requirement_id}",
                "token_id": f"GEO-CORPUS-{uuid.uuid4().hex[:6]}",
                "axis": "vision",
                "title": f"Non-Profit Corpus Knowledge: {knowledge_type} at {target_location}",
                "category": "non_profit_corpus_expansion",
                "contributor_wallet": "0x000000000000000000000000000000000000dcc1",
                "user_id": non_profit_entity,
                "shard_count": shards_rewarded,
                "quality_score": precision_score,
                "metadata": {
                    "location": target_location,
                    "knowledge_type": knowledge_type,
                    "photos_count": photos_count,
                    "corpus_nodes_added": corpus_nodes_added,
                    "non_profit": True,
                },
            }
            wls_engine_bridge._http_post_json(f"{gard_host}/api/v1/marketplace/tokenize", tok_payload, timeout=2.0)
        except Exception as exc:
            logger.debug("Geograph OCR non-profit tokenization deferred: %s", exc)

        scb_link.log_learning(
            "marketplace_geograph_ocr_corpus_expansion",
            f"Geograph OCR expanded non-profit Corpus for {non_profit_entity}: {corpus_nodes_added} relational nodes at {target_location} ({knowledge_type})",
            signal.to_dict(),
            signal=0.92,
        )

        # Feed to QUIPU
        quipu_obs = {
            "source": "loadopoly-ocr",
            "text": f"Geograph OCR acquired relational knowledge for {non_profit_entity} at {target_location}: {knowledge_type}, precision {precision_score:.3f}.",
            "meta": {
                "relational": {
                    "requirement_id": requirement_id,
                    "location": target_location,
                    "precision": precision_score,
                    "nodes_added": corpus_nodes_added,
                    "stream": "geograph_ocr_corpus_expansion",
                }
            },
        }
        self.safety_net.execute(
            f"{wls_engine_bridge.DEFAULT_QUIPU_HOST}/observe",
            quipu_obs,
            wls_engine_bridge._http_post_json,
        )

        events.record("geograph_ocr_corpus_expansion", signal.to_dict(), title=f"Geograph OCR for {non_profit_entity}", signal=0.90)
        return signal

    # -------------------------------------------------------------------------
    # Composite Index & Status
    # -------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Calculates Macro-Marketplace Equilibrium Index and reports status across all 4 streams."""
        m_count = len(self._moltbot_signals)
        ge_count = len(self._ge_signals)
        hub_count = len(self._hub_requirements)
        geo_count = len(self._geograph_ocr_signals)

        # Activity levels in [0.0, 1.0]
        s_moltbot = min(1.0, 0.2 + 0.8 * (m_count / 5.0)) if m_count > 0 else 0.5
        s_ge = min(1.0, 0.3 + 0.7 * (ge_count / 5.0)) if ge_count > 0 else 0.5
        s_hub = min(1.0, 0.3 + 0.7 * (hub_count / 5.0)) if hub_count > 0 else 0.5
        s_geo = min(1.0, 0.2 + 0.8 * (geo_count / 5.0)) if geo_count > 0 else 0.5

        # Weighted composite macro index
        macro_index = round(0.25 * s_moltbot + 0.25 * s_ge + 0.25 * s_hub + 0.25 * s_geo, 4)

        return {
            "macro_marketplace_index": macro_index,
            "counts": {
                "user_moltbot": m_count,
                "ge_maintenance": ge_count,
                "hub_requirements": hub_count,
                "geograph_ocr": geo_count,
            },
            "scores": {
                "moltbot_score": round(s_moltbot, 3),
                "ge_score": round(s_ge, 3),
                "hub_score": round(s_hub, 3),
                "geo_score": round(s_geo, 3),
            },
            "streams": {
                "user_moltbot": {
                    "count": m_count,
                    "activity_score": round(s_moltbot, 3),
                    "recent": [s.to_dict() for s in self._moltbot_signals[-5:]],
                },
                "ge_maintenance": {
                    "count": ge_count,
                    "activity_score": round(s_ge, 3),
                    "recent": [s.to_dict() for s in self._ge_signals[-5:]],
                },
                "hub_existential_requirements": {
                    "count": hub_count,
                    "activity_score": round(s_hub, 3),
                    "recent": [s.to_dict() for s in self._hub_requirements[-5:]],
                },
                "geograph_ocr_corpus": {
                    "count": geo_count,
                    "activity_score": round(s_geo, 3),
                    "recent": [s.to_dict() for s in self._geograph_ocr_signals[-5:]],
                },
            },
            "attenuation": self.attenuation.status(),
            "safety_net": self.safety_net.status(),
            "updated_at": _now(),
        }


_DEFAULT_SIGNALS_ENGINE: Optional[EcosystemMarketplaceSignalsEngine] = None


def get_marketplace_signals_engine() -> EcosystemMarketplaceSignalsEngine:
    global _DEFAULT_SIGNALS_ENGINE
    if _DEFAULT_SIGNALS_ENGINE is None:
        _DEFAULT_SIGNALS_ENGINE = EcosystemMarketplaceSignalsEngine()
    return _DEFAULT_SIGNALS_ENGINE
