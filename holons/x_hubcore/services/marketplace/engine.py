"""Web3 GARD-Shard Shared Marketplace Engine.

Theoretical & Architectural Foundations:
1. Split-View Asset Catalog spanning:
   - Vision Axis (Loadopoly-OCR): Document OCR, historical records, bounding boxes, text transcriptions, genesis docs.
   - Touch Axis (Bakugo CardCenter): Centering metrology, Snell optical refraction, Cramer-Rao / photon shot noise audits, grade ceilings.
   - Unified Shared Plane: Cross-application asset discovery, orderbook, trade settlement, and portfolio ledger.
2. Cryptographic GARD-Shard Protocol:
   - Implements authenticated sharding under 'gard-shard/v2' (AES-256-GCM) with fallback to 'gard-shard/v1' (AES-256-CBC + HMAC-SHA256).
   - Domain: 'SiCi_SQRT(-1)'.
   - Model Context binding: MESH-SLM-SCM-GLM-Quipu-GNN, Weyl tensor coordinates, and Langevin diffusion references.
3. Web3 & On-Chain Shard Coordination:
   - DCC1 Shard smart contract tokenization (218 shards per asset default).
   - On-chain mint tracking, wallet binding, and EIP-6963 provider signature records.
4. Governed 3-Tier Royalty Distribution:
   - 50% Community Fund, 30% Token/Shard Holders, 20% System Maintenance.
5. Governed Data Plane (Part 7.1, two tiers split by mutability):
   - SQLite WAL holds mutable current state: tokenized_assets, shard_holdings,
     pending_rewards. A shard balance is a read-modify-write under concurrency.
   - DuckDB + Hive-partitioned Parquet holds append-only history: royalty
     transactions, hub floor sync records, and envelope metadata.
   - A content-addressed blob store holds envelope payload bytes, which would
     otherwise sit as megabytes of base64 in a SQLite TEXT column.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import sqlite3
import struct
import time
import zlib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

# Protocol Constants
GARD_PROTOCOL = "gard-shard/v2"
GARD_PROTOCOL_VERSION = 2
GARD_ENVELOPE_SCHEMA = "gard-shard-envelope/v2"
GARD_PROTOCOL_V1 = "gard-shard/v1"
GARD_PROTOCOL_VERSION_V1 = 1
GARD_ENVELOPE_SCHEMA_V1 = "gard-shard-envelope/v1"
GARD_SHARD_DOMAIN = "SiCi_SQRT(-1)"
DEFAULT_SHARDS_PER_ASSET = 218
GRID_SECRET_ENV = "SCBRAIN_GRID_SECRET"
DEFAULT_GRID_SECRET = "hideout-sovereign-grid-secret-v2"

_AES_KEY_BYTES = 32
_HMAC_KEY_BYTES = 32
_SALT_BYTES = 32
_IV_BYTES = 16
_GCM_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(value: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(bytes(value)).hexdigest()


def _binary_record(*fields: bytes) -> bytes:
    out = bytearray(b"GARD\x00")
    for f in fields:
        raw = bytes(f)
        out.extend(struct.pack(">I", len(raw)))
        out.extend(raw)
    return bytes(out)


@dataclass
class TokenizedAsset:
    asset_id: str
    token_id: str
    axis: str  # 'vision' | 'touch' | 'entirety'
    title: str
    category: str
    contributor_wallet: str
    user_id: str
    shard_count: int = DEFAULT_SHARDS_PER_ASSET
    available_shards: int = DEFAULT_SHARDS_PER_ASSET
    shard_price_base: float = 1.0
    current_shard_price: float = 1.0
    royalty_rate: float = 0.05
    is_genesis: bool = False
    quality_score: float = 0.95
    precision_score: float = 0.95
    marketplace_scope: str = "shared"  # 'shared' | 'hub'
    org_id: Optional[str] = None
    hub_id: Optional[str] = None
    supply_chain_domain: Optional[str] = None  # 'ownership' | 'forecasting' | 'sales' | 'inventory' | 'procurement' | 'logistics'
    sku_ref: Optional[str] = None
    batch_id: Optional[str] = None
    forecast_horizon: Optional[str] = None
    sla_target: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now_iso)
    last_traded_at: Optional[str] = None
    on_chain_minted: bool = False
    tx_hash: Optional[str] = None
    contract_address: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HubFloorSyncRecord:
    sync_id: str
    hub_id: str
    org_id: str
    worker_id: str
    action: str
    payload_hash: str
    status: str
    timestamp: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ShardHolding:
    id: str
    user_id: str
    asset_id: str
    token_id: str
    shard_count: int
    acquisition_price: float
    acquisition_date: str = field(default_factory=_utc_now_iso)
    current_value: float = 0.0
    unrealized_gain: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RoyaltyTransaction:
    id: str
    asset_id: str
    token_id: str
    transaction_type: str  # 'MINT' | 'SALE' | 'LICENSE'
    sale_price: float
    royalty_amount: float
    community_share: float
    holder_share: float
    maintenance_share: float
    seller_wallet: str
    buyer_wallet: str
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None
    chain_id: int = 137  # Polygon Mainnet
    timestamp: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


#: Which applications each marketplace scope drives. The scope split is not a
#: label on a row: it is which family of applications the information reaches.
#: `shared` is the public cross-application catalog spanning the Vision and
#: Touch axes; `hub` is organization-internal supply chain and never leaves it.
SCOPE_ROUTING: Dict[str, Dict[str, Any]] = {
    "shared": {
        "audience": "public",
        "applications": ("loadopoly-ocr", "bakugo"),
        "axes": ("vision", "touch"),
        "description": "Cross-application public catalog: OCR capture and CardCenter metrology",
        "tenantConfined": False,
    },
    "hub": {
        "audience": "organization",
        "applications": ("hub-floor", "hubcore"),
        "axes": ("entirety",),
        "description": "Organization-internal supply chain shards, confined to one org",
        "tenantConfined": True,
    },
}


def routing_for(scope: str) -> Dict[str, Any]:
    """Where a scope's information is allowed to travel."""
    normalized = (scope or "").strip().lower()
    if normalized not in SCOPE_ROUTING:
        raise ValueError(
            "unknown marketplace scope " + repr(scope) + "; expected one of "
            + ", ".join(sorted(SCOPE_ROUTING))
        )
    return {"scope": normalized, **SCOPE_ROUTING[normalized]}


#: The only two marketplace scopes. Anything else is a caller error, not a
#: reason to widen the query: this list is the hard split.
VALID_SCOPES = frozenset({"shared", "hub"})


from .lakehouse import Lakehouse, LakehouseUnavailable


class GARDMarketplaceEngine:
    """Core state and governance engine for the Web3 GARD-Shard Shared Marketplace."""

    def __init__(self, db_path: Optional[Path] = None, data_dir: Optional[Path] = None):
        self.db_path = Path(db_path or "Data/hub/marketplace.sqlite").resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(data_dir or "Data/parquet/marketplace").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Tier 2. `writer_id` is how a second appender gets refused rather than
        # silently interleaving Parquet files (V10-DAT-004).
        self.lakehouse = Lakehouse(
            self.data_dir,
            writer_id=os.environ.get("MARKETPLACE_WRITER_ID", "gard-marketplace"),
        )
        self.lakehouse.claim_writer()
        # Migration runs first: _init_db creates indexes over columns that an
        # older database does not have yet, so it cannot be the first statement
        # to touch an existing file.
        self._migrate_schema()
        self._init_db()
        self._seed_default_catalog_if_empty()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    #: Columns added after the first release. CREATE TABLE IF NOT EXISTS is a
    #: no-op against an existing table, so a database written by an earlier
    #: version keeps its old shape and every query naming a new column fails.
    #: That is why a fresh test database passed while a real deployment 503'd.
    _ADDED_COLUMNS = (
        ("tokenized_assets", "marketplace_scope", "TEXT DEFAULT 'shared'"),
        ("tokenized_assets", "org_id", "TEXT"),
        ("tokenized_assets", "hub_id", "TEXT"),
        ("tokenized_assets", "supply_chain_domain", "TEXT"),
        ("tokenized_assets", "sku_ref", "TEXT"),
        ("tokenized_assets", "batch_id", "TEXT"),
        ("tokenized_assets", "forecast_horizon", "TEXT"),
        ("tokenized_assets", "sla_target", "TEXT"),
    )

    # -- tier 2: append-only history ---------------------------------------

    #: Tables that moved to the lakehouse. They stay declared in SQLite so an
    #: older database still opens, but nothing writes to them any more.
    _LAKEHOUSE_TABLES = {
        "royalty_transactions": "royalty_transactions",
        "hub_floor_sync_records": "hub_floor_sync_records",
        "gard_shard_envelopes": "gard_envelopes",
    }

    def _audit(self, dataset: str, row: Dict[str, Any]) -> None:
        """Append one append-only row to the lakehouse.

        Never raises into the caller. A royalty distribution or a shard transfer
        has already happened in the transactional tier by the time this runs;
        losing the audit row is bad, but failing the operation that generated it
        would be worse and would leave the two tiers disagreeing.
        """
        try:
            self.lakehouse.append(dataset, row)
        except Exception:
            logger.exception("lakehouse append failed for %s", dataset)

    def _audit_scalar(self, sql: str, default: Dict[str, Any]) -> Dict[str, Any]:
        """One aggregate row from tier 2, or the stated default if it is absent.

        A dashboard counter is not worth failing a whole stats call over, but the
        default is explicit here rather than an empty dict that reads as zero by
        accident.
        """
        try:
            rows = self._audit_query(sql)
        except LakehouseUnavailable:
            return dict(default)
        return dict(rows[0]) if rows else dict(default)

    def _audit_query(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        """Read the append-only tier. Refuses rather than returning a mock row."""
        return self.lakehouse.query(sql, params)

    def migrate_history_to_lakehouse(self) -> Dict[str, Any]:
        """Move append-only rows out of the transactional store, once.

        Kept as an explicit, idempotent operation rather than something that
        happens at import: moving an audit trail is not a side effect.
        """
        moved: Dict[str, int] = {}
        with self._conn() as conn:
            for table, dataset in self._LAKEHOUSE_TABLES.items():
                try:
                    rows = conn.execute("SELECT * FROM " + table).fetchall()
                except Exception:
                    continue
                count = 0
                for row in rows:
                    record = {k: row[k] for k in row.keys()}
                    record.setdefault("created_at", record.get("timestamp") or _utc_now_iso())
                    if dataset == "gard_envelopes" and record.get("envelope_json"):
                        # Payload bytes belong in the blob store, not in a row.
                        try:
                            ref = self.lakehouse.blobs.put(
                                str(record.pop("envelope_json")).encode("utf-8"),
                                "application/json",
                            )
                            record["envelope_blob"] = ref.digest
                            record["envelope_bytes"] = ref.size_bytes
                        except Exception:
                            logger.exception("blob write failed during migration")
                    self._audit(dataset, record)
                    count += 1
                if count:
                    conn.execute("DELETE FROM " + table)
                moved[table] = count
            conn.commit()
        return {"moved": moved, "at": _utc_now_iso()}

    def _migrate_schema(self) -> None:
        """Bring an existing database up to the current column set."""
        with self._conn() as conn:
            for table, column, decl in self._ADDED_COLUMNS:
                try:
                    present = {
                        r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")
                    }
                except Exception:
                    continue
                if not present or column in present:
                    continue
                conn.execute(
                    "ALTER TABLE " + table + " ADD COLUMN " + column + " " + decl
                )
            conn.commit()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tokenized_assets (
                    asset_id TEXT PRIMARY KEY,
                    token_id TEXT UNIQUE NOT NULL,
                    axis TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    contributor_wallet TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    shard_count INTEGER NOT NULL DEFAULT 218,
                    available_shards INTEGER NOT NULL DEFAULT 218,
                    shard_price_base REAL NOT NULL DEFAULT 1.0,
                    current_shard_price REAL NOT NULL DEFAULT 1.0,
                    royalty_rate REAL NOT NULL DEFAULT 0.05,
                    is_genesis INTEGER NOT NULL DEFAULT 0,
                    quality_score REAL NOT NULL DEFAULT 0.95,
                    precision_score REAL NOT NULL DEFAULT 0.95,
                    marketplace_scope TEXT NOT NULL DEFAULT 'shared',
                    org_id TEXT,
                    hub_id TEXT,
                    supply_chain_domain TEXT,
                    sku_ref TEXT,
                    batch_id TEXT,
                    forecast_horizon TEXT,
                    sla_target TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    last_traded_at TEXT,
                    on_chain_minted INTEGER NOT NULL DEFAULT 0,
                    tx_hash TEXT,
                    contract_address TEXT
                );

                CREATE TABLE IF NOT EXISTS shard_holdings (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    shard_count INTEGER NOT NULL,
                    acquisition_price REAL NOT NULL,
                    acquisition_date TEXT NOT NULL,
                    FOREIGN KEY (asset_id) REFERENCES tokenized_assets(asset_id) ON DELETE CASCADE,
                    UNIQUE(user_id, token_id)
                );

                CREATE TABLE IF NOT EXISTS royalty_transactions (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    transaction_type TEXT NOT NULL,
                    sale_price REAL NOT NULL,
                    royalty_amount REAL NOT NULL,
                    community_share REAL NOT NULL,
                    holder_share REAL NOT NULL,
                    maintenance_share REAL NOT NULL,
                    seller_wallet TEXT NOT NULL,
                    buyer_wallet TEXT NOT NULL,
                    tx_hash TEXT,
                    block_number INTEGER,
                    chain_id INTEGER NOT NULL DEFAULT 137,
                    timestamp TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_rewards (
                    user_id TEXT PRIMARY KEY,
                    pending_amount REAL NOT NULL DEFAULT 0.0,
                    last_updated TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gard_shard_envelopes (
                    envelope_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    model_digest TEXT NOT NULL,
                    shard_count INTEGER NOT NULL,
                    envelope_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hub_floor_sync_records (
                    sync_id TEXT PRIMARY KEY,
                    hub_id TEXT NOT NULL,
                    org_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_mkt_scope_axis ON tokenized_assets(marketplace_scope, axis);
                CREATE INDEX IF NOT EXISTS idx_mkt_scope_org ON tokenized_assets(marketplace_scope, org_id);
                CREATE INDEX IF NOT EXISTS idx_mkt_domain ON tokenized_assets(supply_chain_domain);
                """
            )

            # Auto-migrate existing tables if columns missing
            cur = conn.execute("PRAGMA table_info(tokenized_assets)")
            existing_cols = {row["name"] for row in cur.fetchall()}
            migrations = [
                ("marketplace_scope", "TEXT NOT NULL DEFAULT 'shared'"),
                ("org_id", "TEXT"),
                ("hub_id", "TEXT"),
                ("supply_chain_domain", "TEXT"),
                ("sku_ref", "TEXT"),
                ("batch_id", "TEXT"),
                ("forecast_horizon", "TEXT"),
                ("sla_target", "TEXT"),
            ]
            for col_name, col_def in migrations:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE tokenized_assets ADD COLUMN {col_name} {col_def}")

    def _seed_default_catalog_if_empty(self) -> None:
        """Seed representative assets spanning both Shared (OCR / Metrology) and Hub (Supply Chain) marketplaces."""
        with self._conn() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM tokenized_assets")
            if cur.fetchone()[0] > 0:
                return

        # 1. Vision Axis (Loadopoly-OCR) Sample Genesis Document (Shared Marketplace)
        self.tokenize_asset(
            asset_id="doc-genesis-ocr-001",
            token_id="tok-dcc1-ocr-001",
            axis="vision",
            title="1928 Parsons Supply Chain Ledger Manifest (Genesis OCR)",
            category="historical_document",
            contributor_wallet="0x71C7656EC7ab88b098defB751B7401B5f6d89A21",
            user_id="user-loadopoly-ocr-admin",
            shard_count=218,
            shard_price_base=5.0,
            is_genesis=True,
            quality_score=0.985,
            precision_score=0.991,
            marketplace_scope="shared",
            metadata={
                "ocr_engine": "Tesseract+LayoutLMv3",
                "char_count": 4820,
                "confidence": 0.985,
                "bounding_boxes_count": 142,
                "provenance": "Parsons 1928 Archive",
                "source_app": "Loadopoly-OCR",
            },
        )

        # 2. Touch Axis (Bakugo CardCenter) Sample Metrology Scan (Shared Marketplace)
        self.tokenize_asset(
            asset_id="card-metrology-charizard-001",
            token_id="tok-dcc1-bakugo-001",
            axis="touch",
            title="1999 Base Set 1st Edition Charizard Holographic (Sub-Micron Centering Metrology)",
            category="cardcenter_metrology",
            contributor_wallet="0x89A21B7401B5f6d871C7656EC7ab88b098defB75",
            user_id="user-bakugo-metrology-admin",
            shard_count=218,
            shard_price_base=25.0,
            is_genesis=True,
            quality_score=0.996,
            precision_score=0.998,
            marketplace_scope="shared",
            metadata={
                "worst_ratio": 50.8,
                "ratio_ci": [50.2, 51.4],
                "axis": "horizontal",
                "cramer_rao_floor_px": 0.042,
                "snell_refraction": "PSA Slab Grade-A Optical Corrected",
                "grade_ceiling": "PSA 10 Gem Mint",
                "source_app": "Bakugo",
            },
        )

        # 3. Organization Internal Hub Marketplace: Demand Forecast Shard
        self.tokenize_supply_chain_asset(
            asset_id="forecast-freight-q4-2026",
            token_id="tok-hub-fc-2026-q4",
            title="Q4 2026 Multi-Modal Freight Demand & Throughput Forecast Model",
            domain="forecasting",
            contributor_wallet="0x4444444444444444444444444444444444444444",
            user_id="hub-floor-planner-01",
            org_id="org-blackbulls-hideout-01",
            hub_id="hub-floor-main",
            sku_ref="SKU-LOGISTICS-Q4",
            forecast_horizon="90-days",
            shard_count=500,
            shard_price_base=10.0,
            metadata={
                "model_type": "Probabilistic-DeepAR+Ensemble",
                "mape_target": 0.042,
                "confidence_interval": 0.95,
                "seasonal_factors": [1.18, 1.25, 1.40],
            },
        )

        # 4. Organization Internal Hub Marketplace: Warehouse Inventory Batch Lot
        self.tokenize_supply_chain_asset(
            asset_id="lot-warehouse-pallet-alpha88",
            token_id="tok-hub-inv-alpha88",
            title="Bonded High-Bay Terminal Inventory Lot Alpha-88 Shard Allocation",
            domain="inventory",
            contributor_wallet="0x5555555555555555555555555555555555555555",
            user_id="hub-floor-warehouse-mgr",
            org_id="org-blackbulls-hideout-01",
            hub_id="hub-floor-main",
            sku_ref="SKU-CONTAINER-40FT",
            batch_id="BATCH-2026-08-A",
            shard_count=1000,
            shard_price_base=2.50,
            metadata={
                "warehouse_id": "stockopoly-wh-01",
                "storage_zone": "Zone-A-Bonded",
                "pallet_count": 48,
                "rfid_batch_tag": "TAG-ALPHA-88-SECURE",
            },
        )

        # 5. Organization Internal Hub Marketplace: Procurement & SLA Governance Shard
        self.tokenize_supply_chain_asset(
            asset_id="sla-procure-carrier-tier1",
            token_id="tok-hub-sla-carrier01",
            title="Tier-1 Global Ocean Freight SLA Performance Guarantee Shard",
            domain="procurement",
            contributor_wallet="0x6666666666666666666666666666666666666666",
            user_id="hub-floor-procure-lead",
            org_id="org-blackbulls-hideout-01",
            hub_id="hub-floor-main",
            sla_target="99.8% On-Time Delivery / Zero Port Detention",
            shard_count=250,
            shard_price_base=50.0,
            metadata={
                "carrier_code": "MAERSK-GARD-LINE",
                "penalty_clause_bps": 150,
                "arbitration": "HubCore-Airlock-Automated",
            },
        )

    # -------------------------------------------------------------------------
    # Catalog & Tokenization
    # -------------------------------------------------------------------------

    def tokenize_asset(
        self,
        asset_id: str,
        token_id: str,
        axis: str,
        title: str,
        category: str,
        contributor_wallet: str,
        user_id: str,
        shard_count: int = DEFAULT_SHARDS_PER_ASSET,
        shard_price_base: float = 1.0,
        royalty_rate: float = 0.05,
        is_genesis: bool = False,
        quality_score: float = 0.95,
        precision_score: float = 0.95,
        marketplace_scope: str = "shared",
        org_id: Optional[str] = None,
        hub_id: Optional[str] = None,
        supply_chain_domain: Optional[str] = None,
        sku_ref: Optional[str] = None,
        batch_id: Optional[str] = None,
        forecast_horizon: Optional[str] = None,
        sla_target: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TokenizedAsset:
        """Tokenize a data asset into fractional shards spanning Shared or Hub internal marketplaces."""
        axis_norm = axis.lower().strip()
        if axis_norm not in ("vision", "touch", "entirety"):
            raise ValueError(f"axis must be 'vision', 'touch', or 'entirety', got: {axis}")

        scope_norm = marketplace_scope.lower().strip()
        if scope_norm not in ("shared", "hub"):
            raise ValueError(f"marketplace_scope must be 'shared' or 'hub', got: {marketplace_scope}")

        meta = metadata or {}
        now = _utc_now_iso()

        asset = TokenizedAsset(
            asset_id=asset_id,
            token_id=token_id,
            axis=axis_norm,
            title=title,
            category=category,
            contributor_wallet=contributor_wallet,
            user_id=user_id,
            shard_count=int(shard_count),
            available_shards=int(shard_count),
            shard_price_base=float(shard_price_base),
            current_shard_price=float(shard_price_base),
            royalty_rate=float(royalty_rate),
            is_genesis=bool(is_genesis),
            quality_score=float(quality_score),
            precision_score=float(precision_score),
            marketplace_scope=scope_norm,
            org_id=org_id,
            hub_id=hub_id,
            supply_chain_domain=supply_chain_domain,
            sku_ref=sku_ref,
            batch_id=batch_id,
            forecast_horizon=forecast_horizon,
            sla_target=sla_target,
            metadata=meta,
            created_at=now,
        )

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tokenized_assets (
                    asset_id, token_id, axis, title, category, contributor_wallet, user_id,
                    shard_count, available_shards, shard_price_base, current_shard_price,
                    royalty_rate, is_genesis, quality_score, precision_score,
                    marketplace_scope, org_id, hub_id, supply_chain_domain, sku_ref, batch_id, forecast_horizon, sla_target,
                    metadata_json, created_at, on_chain_minted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(asset_id) DO UPDATE SET
                    title=excluded.title,
                    current_shard_price=excluded.current_shard_price,
                    marketplace_scope=excluded.marketplace_scope,
                    org_id=excluded.org_id,
                    hub_id=excluded.hub_id,
                    supply_chain_domain=excluded.supply_chain_domain,
                    sku_ref=excluded.sku_ref,
                    batch_id=excluded.batch_id,
                    forecast_horizon=excluded.forecast_horizon,
                    sla_target=excluded.sla_target,
                    metadata_json=excluded.metadata_json
                """,
                (
                    asset.asset_id,
                    asset.token_id,
                    asset.axis,
                    asset.title,
                    asset.category,
                    asset.contributor_wallet,
                    asset.user_id,
                    asset.shard_count,
                    asset.available_shards,
                    asset.shard_price_base,
                    asset.current_shard_price,
                    asset.royalty_rate,
                    1 if asset.is_genesis else 0,
                    asset.quality_score,
                    asset.precision_score,
                    asset.marketplace_scope,
                    asset.org_id,
                    asset.hub_id,
                    asset.supply_chain_domain,
                    asset.sku_ref,
                    asset.batch_id,
                    asset.forecast_horizon,
                    asset.sla_target,
                    json.dumps(asset.metadata),
                    asset.created_at,
                ),
            )

        return asset

    def tokenize_supply_chain_asset(
        self,
        asset_id: str,
        token_id: str,
        title: str,
        domain: str,
        contributor_wallet: str,
        user_id: str,
        org_id: str = "org-blackbulls-hideout-01",
        hub_id: str = "hub-floor-main",
        sku_ref: Optional[str] = None,
        batch_id: Optional[str] = None,
        forecast_horizon: Optional[str] = None,
        sla_target: Optional[str] = None,
        shard_count: int = 500,
        shard_price_base: float = 10.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TokenizedAsset:
        """Helper to tokenize an Organization Internal Supply Chain Shard Asset into the Hub Marketplace."""
        valid_domains = ("ownership", "forecasting", "sales", "inventory", "procurement", "logistics")
        domain_norm = domain.lower().strip()
        if domain_norm not in valid_domains:
            raise ValueError(f"domain must be one of {valid_domains}, got: {domain}")

        return self.tokenize_asset(
            asset_id=asset_id,
            token_id=token_id,
            axis="entirety",
            title=title,
            category=f"supply_chain_{domain_norm}",
            contributor_wallet=contributor_wallet,
            user_id=user_id,
            shard_count=shard_count,
            shard_price_base=shard_price_base,
            royalty_rate=0.02,  # Internal org governance rate
            is_genesis=False,
            quality_score=0.98,
            marketplace_scope="hub",
            org_id=org_id,
            hub_id=hub_id,
            supply_chain_domain=domain_norm,
            sku_ref=sku_ref,
            batch_id=batch_id,
            forecast_horizon=forecast_horizon,
            sla_target=sla_target,
            metadata=metadata,
        )

    def get_catalog(
        self,
        scope: str = "shared",
        axis: Optional[str] = None,
        org_id: Optional[str] = None,
        domain: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[TokenizedAsset]:
        """Fetch the tokenized asset catalog, split hard between Shared and Hub.

        The split fails **closed**. An earlier revision applied the scope filter
        only when the value was recognised, so ``scope=all``, ``scope=``, or any
        typo dropped the filter entirely and returned every organization's
        internal hub assets alongside the public catalog. It also compared the
        raw string when deciding whether to confine by ``org_id`` while
        filtering on the lower-cased one, so ``scope=HUB`` selected hub assets
        and skipped the tenant confinement -- a cross-organization read from a
        query parameter (V10-ALC-001).
        """
        normalized = (scope or "").strip().lower()
        if normalized not in VALID_SCOPES:
            raise ValueError(
                "unknown marketplace scope " + repr(scope) + "; expected one of "
                + ", ".join(sorted(VALID_SCOPES))
            )
        if normalized == "hub" and not (org_id or "").strip():
            raise ValueError(
                "the org hub marketplace is tenant-confined; an orgId is required "
                "and there is no cross-organization hub listing"
            )

        query = "SELECT * FROM tokenized_assets WHERE 1=1"
        params: List[Any] = []

        query += " AND marketplace_scope = ?"
        params.append(normalized)

        if normalized == "hub":
            # Confinement is unconditional for hub scope. An asset with no
            # org_id is shared infrastructure, never another tenant's record.
            query += " AND (org_id = ? OR org_id IS NULL)"
            params.append(org_id.strip())

        if domain:
            query += " AND supply_chain_domain = ?"
            params.append(domain.lower().strip())

        if axis and axis != "all":
            query += " AND axis = ?"
            params.append(axis.lower().strip())

        if category:
            query += " AND category = ?"
            params.append(category.strip())

        query += " ORDER BY is_genesis DESC, created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))

        with self._conn() as conn:
            cur = conn.execute(query, params)
            rows = cur.fetchall()

        return [self._row_to_asset(r) for r in rows]

    def _row_to_asset(self, r: Any) -> TokenizedAsset:
        keys = r.keys() if hasattr(r, "keys") else []
        return TokenizedAsset(
            asset_id=r["asset_id"],
            token_id=r["token_id"],
            axis=r["axis"],
            title=r["title"],
            category=r["category"],
            contributor_wallet=r["contributor_wallet"],
            user_id=r["user_id"],
            shard_count=r["shard_count"],
            available_shards=r["available_shards"],
            shard_price_base=r["shard_price_base"],
            current_shard_price=r["current_shard_price"],
            royalty_rate=r["royalty_rate"],
            is_genesis=bool(r["is_genesis"]),
            quality_score=r["quality_score"],
            precision_score=r["precision_score"],
            marketplace_scope=r["marketplace_scope"] if "marketplace_scope" in keys else "shared",
            org_id=r["org_id"] if "org_id" in keys else None,
            hub_id=r["hub_id"] if "hub_id" in keys else None,
            supply_chain_domain=r["supply_chain_domain"] if "supply_chain_domain" in keys else None,
            sku_ref=r["sku_ref"] if "sku_ref" in keys else None,
            batch_id=r["batch_id"] if "batch_id" in keys else None,
            forecast_horizon=r["forecast_horizon"] if "forecast_horizon" in keys else None,
            sla_target=r["sla_target"] if "sla_target" in keys else None,
            metadata=json.loads(r["metadata_json"] or "{}"),
            created_at=r["created_at"],
            last_traded_at=r["last_traded_at"],
            on_chain_minted=bool(r["on_chain_minted"]),
            tx_hash=r["tx_hash"],
            contract_address=r["contract_address"],
        )

    def get_asset(self, asset_id: str) -> Optional[TokenizedAsset]:
        with self._conn() as conn:
            cur = conn.execute("SELECT * FROM tokenized_assets WHERE asset_id = ? OR token_id = ?", (asset_id, asset_id))
            r = cur.fetchone()
            if not r:
                return None
            return self._row_to_asset(r)

    def execute_hub_internal_transfer(
        self,
        from_user: str,
        to_user: str,
        asset_id: str,
        shard_count: int,
        reason: str = "Hub Floor Internal Shard Allocation",
        org_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Transfer internal supply chain shards between Hub Floor actors / departments (zero royalty)."""
        if shard_count <= 0:
            raise ValueError("shard_count must be positive")

        asset = self.get_asset(asset_id)
        if not asset:
            raise ValueError(f"asset {asset_id} not found")

        if asset.marketplace_scope != "hub":
            raise ValueError(f"asset {asset_id} is not an internal Hub marketplace asset")

        now = _utc_now_iso()
        transfer_id = f"tx-hub-xfer-{secrets.token_hex(8)}"

        with self._conn() as conn:
            # 1. Deduct from from_user or primary allocation
            if from_user == "primary_allocation" or from_user == asset.user_id:
                if asset.available_shards < shard_count:
                    raise ValueError("insufficient primary shard allocation")
                conn.execute(
                    "UPDATE tokenized_assets SET available_shards = available_shards - ?, last_traded_at = ? WHERE asset_id = ?",
                    (shard_count, now, asset.asset_id),
                )
            else:
                cur = conn.execute(
                    "SELECT id, shard_count FROM shard_holdings WHERE user_id = ? AND token_id = ?",
                    (from_user, asset.token_id),
                )
                from_row = cur.fetchone()
                if not from_row or from_row["shard_count"] < shard_count:
                    raise ValueError(f"sender {from_user} has insufficient shard allocation")

                new_count = from_row["shard_count"] - shard_count
                if new_count == 0:
                    conn.execute("DELETE FROM shard_holdings WHERE id = ?", (from_row["id"],))
                else:
                    conn.execute("UPDATE shard_holdings SET shard_count = ? WHERE id = ?", (new_count, from_row["id"]))

            # 2. Credit to to_user
            cur = conn.execute(
                "SELECT id, shard_count FROM shard_holdings WHERE user_id = ? AND token_id = ?",
                (to_user, asset.token_id),
            )
            to_row = cur.fetchone()
            if to_row:
                conn.execute(
                    "UPDATE shard_holdings SET shard_count = shard_count + ? WHERE id = ?",
                    (shard_count, to_row["id"]),
                )
            else:
                holding_id = f"hold-hub-{secrets.token_hex(8)}"
                conn.execute(
                    "INSERT INTO shard_holdings (id, user_id, asset_id, token_id, shard_count, acquisition_price, acquisition_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (holding_id, to_user, asset.asset_id, asset.token_id, shard_count, asset.shard_price_base, now),
                )

        # 3. Record the transfer in the append-only ledger (tier 2, zero royalty).
        self._audit(
            "royalty_transactions",
            {
                "id": transfer_id,
                "asset_id": asset.asset_id,
                "token_id": asset.token_id,
                "transaction_type": "INTERNAL_XFER",
                "sale_price": shard_count * asset.shard_price_base,
                "royalty_amount": 0.0,
                "community_share": 0.0,
                "holder_share": 0.0,
                "maintenance_share": 0.0,
                "seller_wallet": from_user,
                "buyer_wallet": to_user,
                "tx_hash": "hub-internal:" + reason,
                "block_number": None,
                "chain_id": 0,
                "org_id": org_id,
                "timestamp": now,
                "created_at": now,
            },
        )

        return {
            "ok": True,
            "transfer_id": transfer_id,
            "asset_id": asset.asset_id,
            "token_id": asset.token_id,
            "from_user": from_user,
            "to_user": to_user,
            "shard_count": shard_count,
            "reason": reason,
            "timestamp": now,
        }

    def sync_hub_floor_manifest(
        self,
        hub_id: str,
        org_id: str,
        worker_id: str,
        local_allocations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Loose-connection synchronization between central HubCore marketplace and Org Hub Floor instances."""
        now = _utc_now_iso()
        sync_id = f"sync-{secrets.token_hex(8)}"
        allocs = local_allocations or []
        payload_hash = sha256_hex(canonical_json_bytes({"hub_id": hub_id, "allocs": allocs, "time": now}))

        # A manifest sync is a record of something that happened, never a
        # mutation of current state, so it writes only to the append-only tier.
        self._audit(
            "hub_floor_sync_records",
            {
                "sync_id": sync_id,
                "hub_id": hub_id,
                "org_id": org_id,
                "worker_id": worker_id,
                "action": "MANIFEST_SYNC",
                "payload_hash": payload_hash,
                "status": "COMMITTED",
                "allocation_count": len(allocs),
                "timestamp": now,
                "created_at": now,
            },
        )

        # Retrieve active Hub marketplace assets for this organization
        hub_assets = self.get_catalog(scope="hub", org_id=org_id)
        return {
            "ok": True,
            "sync_id": sync_id,
            "hub_id": hub_id,
            "org_id": org_id,
            "timestamp": now,
            "hub_assets_count": len(hub_assets),
            "hub_assets": [a.to_dict() for a in hub_assets],
            "sync_status": "synced",
        }

    def get_hub_stats(self, org_id: Optional[str] = None) -> Dict[str, Any]:
        """Calculate holistic supply chain and internal organization marketplace metrics."""
        query = "SELECT * FROM tokenized_assets WHERE marketplace_scope = 'hub'"
        params: List[Any] = []
        if org_id:
            query += " AND (org_id = ? OR org_id IS NULL)"
            params.append(org_id)

        with self._conn() as conn:
            cur = conn.execute(query, params)
            rows = cur.fetchall()

        # Sync history lives in tier 2 now.
        sync_row = self._audit_scalar(
            "SELECT COUNT(*) AS n, MAX(timestamp) AS latest FROM hub_floor_sync_records",
            default={"n": 0, "latest": None},
        )

        domain_counts: Dict[str, int] = {}
        total_supply_chain_shards = 0
        total_inventory_value = 0.0

        for r in rows:
            domain = r["supply_chain_domain"] or "general"
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            total_supply_chain_shards += r["shard_count"]
            if domain in ("inventory", "sales", "ownership"):
                total_inventory_value += r["shard_count"] * r["current_shard_price"]

        return {
            "ok": True,
            "org_id": org_id or "all_orgs",
            "total_hub_assets": len(rows),
            "domain_breakdown": domain_counts,
            "total_supply_chain_shards": total_supply_chain_shards,
            "total_inventory_value_usd": round(total_inventory_value, 2),
            "hub_sync_records_count": sync_row["n"] or 0,
            "last_hub_sync_at": sync_row["latest"] or None,
            "virtual_warehouses_allowed": False,
            "snowflake_role": "analytical-export-only",
            "routing_algorithm": "weighted-greedy-dijkstra",
            "protocol": GARD_PROTOCOL,
            "domain": GARD_SHARD_DOMAIN,
        }

    def validate_physical_dc_allocation(
        self,
        source_dc: str,
        target_dc: str,
        include_virtual: bool = False,
        storage_location: str = "",
    ) -> Dict[str, Any]:
        """Enforce TX03/TX77 invariant: prohibit virtual warehouses and exclude storage 916."""
        src = str(source_dc or "").strip().upper()
        dst = str(target_dc or "").strip().upper()
        loc = str(storage_location or "").strip()
        if include_virtual or "-IN-" in src or "VIRTUAL" in src or "VIRTUAL" in loc.upper():
            raise ValueError(
                "VIRTUAL_WAREHOUSE_PROHIBITED: Virtual bins (e.g., TX03-in-TX77 / NJ22-in-PA50) cause MRO backorder hijacking and fake AP distortions. Route via Unified Physical Inventory Marketplace + Weighted Greedy Dijkstra."
            )
        if loc == "916":
            raise ValueError("STORAGE_916_EXCLUDED: SAP storage location 916 is excluded from sellable ATP.")
        return {
            "ok": True,
            "sourceDc": src,
            "targetDc": dst,
            "physicalOnly": True,
            "virtualWarehousesAllowed": False,
            "snowflakeRole": "analytical-export-only",
        }

    # -------------------------------------------------------------------------
    # Trading & Shard Orderbook
    # -------------------------------------------------------------------------

    def execute_trade(
        self,
        buyer_id: str,
        seller_id: str,
        asset_id: str,
        shard_count: int,
        unit_price: Optional[float] = None,
        tx_hash: Optional[str] = None,
        block_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute a shard trade with automated 3-tier royalty distribution."""
        if shard_count <= 0:
            raise ValueError("shard_count must be positive")

        asset = self.get_asset(asset_id)
        if not asset:
            raise ValueError(f"asset {asset_id} not found")

        price = float(unit_price if unit_price is not None else asset.current_shard_price)
        total_sale_price = price * shard_count

        # Primary acquisition vs Secondary market
        is_primary = (seller_id == "primary_market" or seller_id == asset.user_id)

        # 3-Tier Royalty Calculation: 5% standard rate
        royalty_amount = round(total_sale_price * asset.royalty_rate, 4)
        community_share = round(royalty_amount * 0.50, 4)
        holder_share = round(royalty_amount * 0.30, 4)
        maintenance_share = round(royalty_amount * 0.20, 4)

        now = _utc_now_iso()
        tx_id = f"tx-royalty-{secrets.token_hex(8)}"

        with self._conn() as conn:
            # 1. Update available shards if primary
            if is_primary:
                if asset.available_shards < shard_count:
                    raise ValueError("insufficient primary shard supply")
                conn.execute(
                    "UPDATE tokenized_assets SET available_shards = available_shards - ?, last_traded_at = ? WHERE asset_id = ?",
                    (shard_count, now, asset.asset_id),
                )
            else:
                # Deduct from seller
                cur = conn.execute(
                    "SELECT shard_count FROM shard_holdings WHERE user_id = ? AND token_id = ?",
                    (seller_id, asset.token_id),
                )
                seller_row = cur.fetchone()
                if not seller_row or seller_row["shard_count"] < shard_count:
                    raise ValueError("seller has insufficient shard balance")

                new_seller_count = seller_row["shard_count"] - shard_count
                if new_seller_count == 0:
                    conn.execute("DELETE FROM shard_holdings WHERE user_id = ? AND token_id = ?", (seller_id, asset.token_id))
                else:
                    conn.execute(
                        "UPDATE shard_holdings SET shard_count = ? WHERE user_id = ? AND token_id = ?",
                        (new_seller_count, seller_id, asset.token_id),
                    )

            # 2. Add / Update buyer holding
            cur = conn.execute(
                "SELECT id, shard_count, acquisition_price FROM shard_holdings WHERE user_id = ? AND token_id = ?",
                (buyer_id, asset.token_id),
            )
            buyer_row = cur.fetchone()
            if buyer_row:
                old_count = buyer_row["shard_count"]
                old_price = buyer_row["acquisition_price"]
                new_count = old_count + shard_count
                new_avg_price = ((old_count * old_price) + (shard_count * price)) / new_count
                conn.execute(
                    "UPDATE shard_holdings SET shard_count = ?, acquisition_price = ? WHERE id = ?",
                    (new_count, new_avg_price, buyer_row["id"]),
                )
            else:
                holding_id = f"hold-{secrets.token_hex(8)}"
                conn.execute(
                    "INSERT INTO shard_holdings (id, user_id, asset_id, token_id, shard_count, acquisition_price, acquisition_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (holding_id, buyer_id, asset.asset_id, asset.token_id, shard_count, price, now),
                )

            # 3. Record Royalty Transaction
            pass  # tier-2 audit row is appended after the transaction commits

            # 4. Accrue pending holder rewards
            conn.execute(
                """
                INSERT INTO pending_rewards (user_id, pending_amount, last_updated)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    pending_amount = pending_amount + excluded.pending_amount,
                    last_updated = excluded.last_updated
                """,
                (asset.user_id, holder_share, now),
            )

        self._audit(
            "royalty_transactions",
            {
                "id": tx_id,
                "asset_id": asset.asset_id,
                "token_id": asset.token_id,
                "transaction_type": "MINT" if is_primary else "SALE",
                "sale_price": total_sale_price,
                "royalty_amount": royalty_amount,
                "community_share": community_share,
                "holder_share": holder_share,
                "maintenance_share": maintenance_share,
                "seller_wallet": asset.contributor_wallet if is_primary else seller_id,
                "buyer_wallet": buyer_id,
                "tx_hash": tx_hash or ("0x" + secrets.token_hex(32)),
                "block_number": block_number or 58941200,
                "chain_id": 137,
                "org_id": asset.org_id,
                "marketplace_scope": asset.marketplace_scope,
                "timestamp": now,
                "created_at": now,
            },
        )

        return {
            "ok": True,
            "transaction_id": tx_id,
            "asset_id": asset.asset_id,
            "token_id": asset.token_id,
            "shard_count": shard_count,
            "unit_price": price,
            "total_price": total_sale_price,
            "royalty": {
                "total": royalty_amount,
                "community_share": community_share,
                "holder_share": holder_share,
                "maintenance_share": maintenance_share,
            },
            "tx_hash": tx_hash,
            "timestamp": now,
        }

    def get_user_portfolio(self, user_id: str) -> List[Dict[str, Any]]:
        """Fetch all shard holdings and voting weights for a given user."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                SELECT h.*, a.title, a.axis, a.category, a.current_shard_price, a.shard_count AS total_asset_shards
                FROM shard_holdings h
                JOIN tokenized_assets a ON h.token_id = a.token_id
                WHERE h.user_id = ?
                """,
                (user_id,),
            )
            rows = cur.fetchall()

        portfolio = []
        for r in rows:
            current_val = r["shard_count"] * r["current_shard_price"]
            cost_basis = r["shard_count"] * r["acquisition_price"]
            unrealized = current_val - cost_basis
            voting_weight = r["shard_count"] / max(1, r["total_asset_shards"])

            portfolio.append(
                {
                    "holding_id": r["id"],
                    "asset_id": r["asset_id"],
                    "token_id": r["token_id"],
                    "title": r["title"],
                    "axis": r["axis"],
                    "category": r["category"],
                    "shard_count": r["shard_count"],
                    "acquisition_price": r["acquisition_price"],
                    "current_price": r["current_shard_price"],
                    "current_value": round(current_val, 2),
                    "unrealized_gain": round(unrealized, 2),
                    "voting_weight": round(voting_weight, 4),
                    "acquisition_date": r["acquisition_date"],
                }
            )
        return portfolio

    def get_recent_transactions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Recent ledger rows, read from the append-only tier."""
        return self._audit_query(
            "SELECT * FROM royalty_transactions ORDER BY timestamp DESC LIMIT ?",
            [max(1, min(limit, 200))],
        )

    def claim_rewards(self, user_id: str) -> float:
        now = _utc_now_iso()
        with self._conn() as conn:
            cur = conn.execute("SELECT pending_amount FROM pending_rewards WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            if not row or row["pending_amount"] <= 0:
                return 0.0
            amount = float(row["pending_amount"])
            conn.execute("UPDATE pending_rewards SET pending_amount = 0.0, last_updated = ? WHERE user_id = ?", (now, user_id))
            return amount

    # -------------------------------------------------------------------------
    # Web3 On-Chain Minting & Synchronization
    # -------------------------------------------------------------------------

    def record_on_chain_mint(
        self,
        asset_id: str,
        tx_hash: str,
        contract_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record an on-chain DCC1 ERC-1155 mint event."""
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE tokenized_assets
                SET on_chain_minted = 1, tx_hash = ?, contract_address = ?
                WHERE asset_id = ? OR token_id = ?
                """,
                (tx_hash, contract_address or "0x71C7656EC7ab88b098defB751B7401B5f6d89A21", asset_id, asset_id),
            )
        return {"ok": True, "asset_id": asset_id, "tx_hash": tx_hash, "on_chain_minted": True}

    # -------------------------------------------------------------------------
    # Cryptographic GARD-Shard Protocol (v1 & v2 AES-GCM / AES-CBC)
    # -------------------------------------------------------------------------

    def create_gard_envelope(
        self,
        asset_id: str,
        payload: Mapping[str, Any],
        shard_count: int = 4,
        secret: Optional[str] = None,
        model_context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create an authenticated, encrypted, and sharded GARD envelope ('gard-shard/v2')."""
        grid_secret = (secret or os.environ.get(GRID_SECRET_ENV) or DEFAULT_GRID_SECRET).encode("utf-8")
        canonical_raw = canonical_json_bytes(payload)
        compressed = zlib.compress(canonical_raw, level=6)

        context = dict(model_context or self._default_model_context())
        model_digest_hex = sha256_hex(canonical_json_bytes(context))
        model_digest_bytes = bytes.fromhex(model_digest_hex)

        count = max(1, min(shard_count, 64))
        chunk_size = (len(compressed) + count - 1) // count
        chunks = [compressed[i : i + chunk_size] for i in range(0, len(compressed), chunk_size)]
        actual_count = len(chunks)

        shards_data = []
        for index, chunk in enumerate(chunks):
            salt = secrets.token_bytes(_SALT_BYTES)
            nonce = secrets.token_bytes(_GCM_NONCE_BYTES)

            # HKDF key derivation
            info = _binary_record(
                b"gard-shard-hkdf/v2",
                GARD_PROTOCOL.encode("utf-8"),
                GARD_SHARD_DOMAIN.encode("utf-8"),
                model_digest_bytes,
                struct.pack(">I", index),
                struct.pack(">I", actual_count),
            )
            key = HKDF(
                algorithm=hashes.SHA256(),
                length=_AES_KEY_BYTES,
                salt=salt,
                info=info,
            ).derive(grid_secret)

            # AES-256-GCM AAD
            aad = _binary_record(
                b"gard-shard-aad/v2",
                GARD_PROTOCOL.encode("utf-8"),
                struct.pack(">I", GARD_PROTOCOL_VERSION),
                GARD_SHARD_DOMAIN.encode("utf-8"),
                struct.pack(">I", 7),
                model_digest_bytes,
                salt,
                struct.pack(">I", index),
                struct.pack(">I", actual_count),
                nonce,
            )

            sealed = AESGCM(key).encrypt(nonce, chunk, aad)

            shards_data.append(
                {
                    "index": index,
                    "count": actual_count,
                    "salt_b64": base64.b64encode(salt).decode("ascii"),
                    "nonce_b64": base64.b64encode(nonce).decode("ascii"),
                    "ciphertext_b64": base64.b64encode(sealed).decode("ascii"),
                    "ciphertext_sha256": sha256_hex(sealed),
                }
            )

        envelope = {
            "schema": GARD_ENVELOPE_SCHEMA,
            "protocol": GARD_PROTOCOL,
            "protocol_version": GARD_PROTOCOL_VERSION,
            "domain": GARD_SHARD_DOMAIN,
            "encoding": "base64",
            "compression": {"algorithm": "zlib", "level": 6},
            "encryption": {"algorithm": "AES-256-GCM", "padding": "none"},
            "authentication": {"algorithm": "AES-256-GCM", "mode": "aead"},
            "model_digest": model_digest_hex,
            "model_context": context,
            "plaintext_sha256": sha256_hex(canonical_raw),
            "compressed_sha256": sha256_hex(compressed),
            "plaintext_bytes": len(canonical_raw),
            "compressed_bytes": len(compressed),
            "shard_count": actual_count,
            "shards": shards_data,
        }

        # Persist the envelope: payload bytes to the blob store, metadata to the
        # append-only tier. The shard ciphertext is immutable once sealed, so the
        # digest is its identity; keeping it inline would put megabytes of base64
        # into a TEXT column for every scan tokenized.
        env_id = f"env-{secrets.token_hex(8)}"
        now = _utc_now_iso()
        blob = self.lakehouse.blobs.put_json(envelope)
        self._audit(
            "gard_envelopes",
            {
                "envelope_id": env_id,
                "asset_id": asset_id,
                "token_id": asset_id,
                "protocol": GARD_PROTOCOL,
                "model_digest": model_digest_hex,
                "shard_count": actual_count,
                "envelope_blob": blob.digest,
                "envelope_bytes": blob.size_bytes,
                "plaintext_sha256": envelope.get("plaintext_sha256"),
                "created_at": now,
            },
        )

        envelope = dict(envelope)
        envelope["envelope_id"] = env_id
        envelope["envelope_blob"] = blob.digest
        return envelope

    def verify_and_reassemble_envelope(
        self,
        envelope: Mapping[str, Any],
        secret: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Verify cryptographic integrity and reassemble original plaintext."""
        grid_secret = (secret or os.environ.get(GRID_SECRET_ENV) or DEFAULT_GRID_SECRET).encode("utf-8")
        protocol = envelope.get("protocol")
        if protocol != GARD_PROTOCOL and protocol != GARD_PROTOCOL_V1:
            raise ValueError(f"unsupported GARD protocol: {protocol}")

        model_digest_hex = envelope.get("model_digest")
        model_digest_bytes = bytes.fromhex(model_digest_hex)
        context = envelope.get("model_context", {})
        if sha256_hex(canonical_json_bytes(context)) != model_digest_hex:
            raise ValueError("GARD model context digest mismatch")

        shards = envelope.get("shards", [])
        total_shards = len(shards)
        compressed_parts = []

        if protocol == GARD_PROTOCOL:
            for s in shards:
                salt = base64.b64decode(s["salt_b64"])
                nonce = base64.b64decode(s["nonce_b64"])
                ciphertext = base64.b64decode(s["ciphertext_b64"])
                index = s["index"]

                info = _binary_record(
                    b"gard-shard-hkdf/v2",
                    GARD_PROTOCOL.encode("utf-8"),
                    GARD_SHARD_DOMAIN.encode("utf-8"),
                    model_digest_bytes,
                    struct.pack(">I", index),
                    struct.pack(">I", total_shards),
                )
                key = HKDF(
                    algorithm=hashes.SHA256(),
                    length=_AES_KEY_BYTES,
                    salt=salt,
                    info=info,
                ).derive(grid_secret)

                aad = _binary_record(
                    b"gard-shard-aad/v2",
                    GARD_PROTOCOL.encode("utf-8"),
                    struct.pack(">I", GARD_PROTOCOL_VERSION),
                    GARD_SHARD_DOMAIN.encode("utf-8"),
                    struct.pack(">I", 7),
                    model_digest_bytes,
                    salt,
                    struct.pack(">I", index),
                    struct.pack(">I", total_shards),
                    nonce,
                )

                try:
                    decrypted_chunk = AESGCM(key).decrypt(nonce, ciphertext, aad)
                    compressed_parts.append(decrypted_chunk)
                except InvalidTag as exc:
                    raise ValueError(f"GARD shard {index} AEAD tag verification failed: tampering detected") from exc
        else:
            # Legacy v1 AES-256-CBC + HMAC-SHA256
            for s in shards:
                salt = base64.b64decode(s["salt_b64"])
                iv = base64.b64decode(s["iv_b64"])
                ciphertext = base64.b64decode(s["ciphertext_b64"])
                mac = base64.b64decode(s["hmac_sha256_b64"])
                index = s["index"]

                info = _binary_record(
                    b"gard-shard-hkdf/v1",
                    GARD_PROTOCOL_V1.encode("utf-8"),
                    GARD_SHARD_DOMAIN.encode("utf-8"),
                    model_digest_bytes,
                    struct.pack(">I", index),
                    struct.pack(">I", total_shards),
                )
                material = HKDF(
                    algorithm=hashes.SHA256(),
                    length=64,
                    salt=salt,
                    info=info,
                ).derive(grid_secret)
                aes_key, hmac_key = material[:32], material[32:]

                preimage = _binary_record(
                    b"gard-shard-mac/v1",
                    GARD_PROTOCOL_V1.encode("utf-8"),
                    struct.pack(">I", GARD_PROTOCOL_VERSION_V1),
                    GARD_SHARD_DOMAIN.encode("utf-8"),
                    struct.pack(">I", 7),
                    model_digest_bytes,
                    salt,
                    struct.pack(">I", index),
                    struct.pack(">I", total_shards),
                    iv,
                    ciphertext,
                )
                expected_mac = hmac.new(hmac_key, preimage, hashlib.sha256).digest()
                if not hmac.compare_digest(mac, expected_mac):
                    raise ValueError(f"GARD v1 shard {index} HMAC verification failed")

                decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
                padded = decryptor.update(ciphertext) + decryptor.finalize()
                unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
                chunk = unpadder.update(padded) + unpadder.finalize()
                compressed_parts.append(chunk)

        compressed = b"".join(compressed_parts)
        if sha256_hex(compressed) != envelope.get("compressed_sha256"):
            raise ValueError("decompressed payload digest mismatch")

        raw_json = zlib.decompress(compressed)
        if sha256_hex(raw_json) != envelope.get("plaintext_sha256"):
            raise ValueError("plaintext JSON digest mismatch")

        return {
            "verified": True,
            "protocol": protocol,
            "shard_count": total_shards,
            "payload": json.loads(raw_json.decode("utf-8")),
            "model_digest": model_digest_hex,
        }

    def _default_model_context(self) -> Dict[str, Any]:
        return {
            "schema": "gard-model-context/v1",
            "architecture": "MESH-SLM-SCM-GLM-Quipu-GNN",
            "mesh_state7": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
            "weyl_psi5": [0.366779, 0.95821, 0.991736, 0.6003, 0.56],
            "gard": {
                "protocol": GARD_PROTOCOL,
                "domain": GARD_SHARD_DOMAIN,
                "verify_digest_default": True,
            },
        }

    # -------------------------------------------------------------------------
    # Marketplace System Posture & Stats
    # -------------------------------------------------------------------------

    def get_stats(self, scope: Optional[str] = None, org_id: Optional[str] = None) -> Dict[str, Any]:
        with self._conn() as conn:
            cur = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_assets,
                    SUM(CASE WHEN marketplace_scope='shared' THEN 1 ELSE 0 END) AS shared_assets,
                    SUM(CASE WHEN marketplace_scope='hub' THEN 1 ELSE 0 END) AS hub_assets,
                    SUM(CASE WHEN axis='vision' THEN 1 ELSE 0 END) AS vision_assets,
                    SUM(CASE WHEN axis='touch' THEN 1 ELSE 0 END) AS touch_assets,
                    SUM(shard_count) AS total_shards,
                    SUM(available_shards) AS available_shards,
                    SUM(current_shard_price * (shard_count - available_shards)) AS total_circulating_volume
                FROM tokenized_assets
                """
            )
            row = cur.fetchone()
        trade_row = self._audit_scalar(
            "SELECT COUNT(*) AS n, SUM(sale_price) AS volume, SUM(royalty_amount) AS royalties"
            " FROM royalty_transactions",
            default={"n": 0, "volume": 0.0, "royalties": 0.0},
        )
        t_row = [trade_row["n"], trade_row["volume"], trade_row["royalties"]]
        sync_count = self._audit_scalar(
            "SELECT COUNT(*) AS n FROM hub_floor_sync_records", default={"n": 0}
        )["n"]

        return {
            "ok": True,
            "total_assets": row["total_assets"] or 0,
            "shared_assets": row["shared_assets"] or 0,
            "hub_assets": row["hub_assets"] or 0,
            "vision_assets": row["vision_assets"] or 0,
            "touch_assets": row["touch_assets"] or 0,
            "total_shards": row["total_shards"] or 0,
            "available_shards": row["available_shards"] or 0,
            "circulating_volume_usd": round(row["total_circulating_volume"] or 0.0, 2),
            "trade_count": t_row[0] or 0,
            "gross_trade_volume_usd": round(t_row[1] or 0.0, 2),
            "total_royalties_distributed_usd": round(t_row[2] or 0.0, 2),
            "hub_floor_sync_count": sync_count or 0,
            "virtual_warehouses_allowed": False,
            "snowflake_role": "analytical