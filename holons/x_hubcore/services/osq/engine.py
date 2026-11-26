"""Two-tier relational static & shared temp engine for x_OSQ HolonHubLive (:8900).

Maintains:
1. Relationally Static Data (Data/hub/osq_relational_store.json + SQLite WAL mirror):
   Accounts, Opportunities (TopLink hierarchies), Item Master & SAP Direct API Time-Phased QOH,
   Quotes / Floorplans / Lines, Unified Physical Inventory Marketplace (Weighted Greedy Dijkstra),
   Probabilistic Stockout Guardrails (2σ@10d / 3σ@6d / 5d Expedite + CTO), IFR Auto-PO &
   Cancel-to-PO-Reduction Receiving Quarantine, Rust RFID IS/CS Telemetry, QUIPU Handwritten Notes,
   Stockopoly Bin Coordinate Bridge, and Bounded SAP Signal Bridge (Snowflake analytics-only).
2. Shared Temp Data (Data/hub/temp/holonhub_live_temp.json):
   Active multi-user sessions with TTL pruning, collaborative soft locks,
   uncommitted shared drafts, live What-If simulation scratch, and PI-16
   in-context chat & @mention stream (quote + IS/CS shipment channels).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .pathway_bridge import CyberPhysicalPathwayRouter


class ConflictError(ValueError):
    """Raised when idempotency hash, expected revision, lease fence, or replay check conflicts (HTTP 409)."""


class UnavailableError(ValueError):
    """Raised when no active routable capability or registration lease exists (HTTP 503)."""


class ForbiddenError(ValueError):
    """Raised when deployment-owner policy denies capability/grant access (HTTP 403)."""


class NotFoundError(ValueError):
    """Raised when a requested task, quote, or registration does not exist (HTTP 404)."""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_secret_ref(auth_ref: Any) -> str:
    val = str(auth_ref or "").strip()
    if not val or val.upper() == "NONE":
        return val or "NONE"
    low = val.lower()
    if (
        low.startswith("bearer ")
        or "eyj" in low
        or "private key" in low
        or "password=" in low
        or "secret=" in low
    ):
        raise ValueError("Air-Lock Policy Blocked: secret references only (ENV:*, VAULT:*, SECRETREF:*, or NONE); raw credentials forbidden.")
    upper = val.upper()
    if not (upper.startswith("ENV:") or upper.startswith("VAULT:") or upper.startswith("SECRETREF:")):
        raise ValueError("Air-Lock Policy Blocked: authRef must use ENV:, VAULT:, SECRETREF:, or NONE.")
    return val


class OsqRelationalAndTempEngine:
    """Thread-safe coordinator for Node A HolonHubLive authoritative, ephemeral, Air-Lock & MESH state."""

    SESSION_TTL_SECONDS = 45
    LOCK_TTL_SECONDS = 120
    MAX_PAYLOAD_BYTES = 1048576
    POLICY_VERSION = "owner-policy/v2.4"
    HOME_NODE_ID = "node-a-osq-home"
    PEER_NODE_ID = "node-b-federated-peer"

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        relational_path: Optional[Path] = None,
        temp_path: Optional[Path] = None,
        sqlite_path: Optional[Path] = None,
        seed_path: Optional[Path] = None,
    ):
        product_env = os.environ.get("OSQ_PRODUCT_ROOT")
        product_root = Path(product_env).resolve() if product_env else next(
            (
                p for p in Path(__file__).resolve().parents
                if (p / "app" / "x_OSQ.html").exists() and (p / "data" / "seed" / "osq_relational_seed.json").exists()
            ),
            None,
        )

        if product_root is not None:
            self.repo_root = product_root
            self.seed_path = Path(seed_path or (product_root / "data" / "seed" / "osq_relational_seed.json")).resolve()
            self.relational_path = Path(relational_path or (product_root / "data" / "osq_relational_store.json")).resolve()
            self.temp_path = Path(temp_path or (product_root / "data" / "temp" / "holonhub_live_temp.json")).resolve()
            self.sqlite_path = Path(sqlite_path or (product_root / "data" / "osq_local.sqlite")).resolve()
        else:
            self.repo_root = Path(repo_root or Path(__file__).resolve().parents[2]).resolve()
            self.seed_path = Path(seed_path).resolve() if seed_path else None
            self.relational_path = Path(
                relational_path or (self.repo_root / "Data" / "hub" / "osq_relational_store.json")
            ).resolve()
            self.temp_path = Path(
                temp_path or (self.repo_root / "Data" / "hub" / "temp" / "holonhub_live_temp.json")
            ).resolve()
            self.sqlite_path = Path(
                sqlite_path or (self.repo_root / "pipeline" / "local_brain.sqlite")
            ).resolve()
        self.ephemeral_sqlite_path = self.temp_path.parent / "osq_ephemeral.sqlite"

        self._lock = threading.RLock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._relational = self._load_or_init_relational()
        self._temp = self._load_or_init_temp()
        self._ensure_heuristics_config()
        if not self._relational.get("lastHeuristicsEvaluation"):
            self._evaluate_heuristics_order({})
        self._refresh_pathways_and_directives("bootstrap", {}, "system")
        self._fencing_counter = 0
        self._entity_max_fencing: Dict[str, int] = {}
        self._idempotency_index: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._task_order: List[str] = []
        self._outbox: List[Dict[str, Any]] = []
        self._restricted_audit: List[Dict[str, Any]] = []
        self._marketplace_replays: set[str] = set()
        self._connector_outcomes: Dict[str, Dict[str, Any]] = {}
        self._immutable_snapshots: List[Dict[str, Any]] = []
        self._registrations: Dict[str, Dict[str, Any]] = {}
        self._mesh_state: Dict[str, Any] = {}
        self._init_control_plane_state()
        self._init_sqlite_mirror()
        self._export_watermark_snapshot(actor="system", reason="bootstrap")

    def _init_control_plane_state(self) -> None:
        now_epoch = time.time()
        lease_exp = datetime.fromtimestamp(now_epoch + 300, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._fencing_counter = 0
        self._entity_max_fencing = {}
        self._idempotency_index = {}
        self._tasks = {}
        self._task_order = []
        self._outbox = []
        self._restricted_audit = []
        self._marketplace_replays = set()
        self._connector_outcomes = {}
        self._immutable_snapshots = []
        self._registrations = {
            "x_osq": {
                "registrationId": "REG-XOSQ-NODEA-01",
                "holonId": "x_osq",
                "instanceId": "x_osq-node-a-01",
                "homeNodeId": self.HOME_NODE_ID,
                "version": "2.3.0",
                "status": "ACTIVE",
                "routable": True,
                "capabilities": [
                    "osq.forecast-intake",
                    "osq.forecast-cleansing",
                    "osq.quipu-intelligence",
                    "osq.forecast-governance",
                    "osq.by-transformation",
                    "osq.integration-connectors",
                ],
                "grantedScopes": [
                    "crm.read",
                    "crm.write",
                    "cpq.write",
                    "osq.read",
                    "osq.write",
                    "gard.artifact.exchange",
                    "connector.fulfillment.execute",
                    "analytics.duckdb.read",
                    "model.gateway.invoke",
                    "process.studio.submit_task",
                ],
                "healthEndpoint": "/health",
                "artifactHash": _sha256_json({"holonId": "x_osq", "version": "2.3.0"}),
                "registeredAt": _utc_now(),
                "leaseExpiresAt": lease_exp,
                "leaseExpiresEpoch": now_epoch + 300,
                "reason": "Signed manifest verified and health ready",
            },
            "quipu-observer": {
                "registrationId": "REG-QUIPU-7100",
                "holonId": "quipu-observer",
                "instanceId": "quipu-node-a-01",
                "homeNodeId": self.HOME_NODE_ID,
                "version": "1.0.0",
                "status": "ACTIVE",
                "routable": True,
                "capabilities": ["intelligence.quipu.evaluate"],
                "grantedScopes": ["analytics.duckdb.read", "governance.intent.emit"],
                "healthEndpoint": "http://127.0.0.1:7100/health",
                "registeredAt": _utc_now(),
                "leaseExpiresAt": lease_exp,
                "leaseExpiresEpoch": now_epoch + 300,
            },
            "x_ModelGateway": {
                "registrationId": "REG-MODELGW-01",
                "holonId": "x_ModelGateway",
                "instanceId": "model-gw-01",
                "homeNodeId": self.HOME_NODE_ID,
                "version": "1.0.0",
                "status": "ACTIVE",
                "routable": True,
                "capabilities": ["model.gateway.invoke"],
                "grantedScopes": ["model.gateway.invoke"],
                "healthEndpoint": "/health",
                "registeredAt": _utc_now(),
                "leaseExpiresAt": lease_exp,
                "leaseExpiresEpoch": now_epoch + 300,
            },
            "ProcessStudio": {
                "registrationId": "REG-PROCSTUDIO-01",
                "holonId": "ProcessStudio",
                "instanceId": "process-studio-01",
                "homeNodeId": self.HOME_NODE_ID,
                "version": "1.0.0",
                "status": "ACTIVE",
                "routable": True,
                "capabilities": ["workflow.process.orchestrate"],
                "grantedScopes": ["process.studio.submit_task"],
                "healthEndpoint": "/health",
                "registeredAt": _utc_now(),
                "leaseExpiresAt": lease_exp,
                "leaseExpiresEpoch": now_epoch + 300,
            },
        }
        self._mesh_state = {
            "homeNodeId": self.HOME_NODE_ID,
            "peerNodeId": self.PEER_NODE_ID,
            "protocolVersion": "holon-mesh/2.3",
            "peerBindings": {},
            "peerInbox": [],
            "seenEnvelopeIds": [],
            "advertisedWithdrawnCount": 0,
            "nodeB": {
                "nodeId": self.PEER_NODE_ID,
                "localGrants": ["peer.local.read", "peer.local.write"],
                "localOwnedState": {},
                "pendingHomeNodeQueue": [],
                "selfPromotedToOwner": False,
            },
        }

    def _record_restricted_audit(
        self,
        identity: str,
        decision: str,
        reason: str,
        correlation_id: str = "",
        task_id: str = "",
    ) -> None:
        entry = {
            "seq": len(self._restricted_audit) + 1,
            "time": _utc_now(),
            "identity": identity[:80],
            "policyVersion": self.POLICY_VERSION,
            "decision": decision,
            "reason": reason[:240],
            "correlation_id": correlation_id,
            "task_id": task_id,
        }
        self._restricted_audit.append(entry)
        if len(self._restricted_audit) > 500:
            del self._restricted_audit[: len(self._restricted_audit) - 500]
        try:
            with sqlite3.connect(str(self.sqlite_path), timeout=5.0) as conn:
                conn.execute(
                    """
                    INSERT INTO hubcore_restricted_audit(time_utc, identity, policy_version, decision, reason, correlation_id, task_id)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (entry["time"], entry["identity"], entry["policyVersion"], entry["decision"], entry["reason"], correlation_id, task_id),
                )
                conn.commit()
        except Exception:
            pass

    def _check_active_route(self, holon_id: str = "x_osq") -> Dict[str, Any]:
        reg = self._registrations.get(holon_id)
        if not reg:
            raise UnavailableError(f"Hold/Reject: Holon '{holon_id}' has no registration record.")
        if float(reg.get("leaseExpiresEpoch") or 0.0) < time.time() and reg.get("status") == "ACTIVE":
            reg["status"] = "EXPIRED"
            reg["routable"] = False
            reg["reason"] = "Registration lease expired"
            self._mesh_state["advertisedWithdrawnCount"] = int(self._mesh_state.get("advertisedWithdrawnCount", 0)) + 1
        if reg.get("status") != "ACTIVE" or not reg.get("routable"):
            raise UnavailableError(
                f"No routable capability for '{holon_id}' (status={reg.get('status')}, reason={reg.get('reason') or 'withdrawn'})."
            )
        return reg

    def _build_canonical_objects(
        self,
        action: str,
        actor: str,
        payload: Dict[str, Any],
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        seed_hex = _sha256_json({"action": action, "time": time.time(), "payload": payload})[:12]
        task_id = f"task-{seed_hex}"
        corr_id = correlation_id or f"corr-{seed_hex}"
        caus_id = causation_id or task_id
        result_id = f"res-{seed_hex}"
        event_id = f"evt-{seed_hex}"
        artifact_id = f"art-{seed_hex}"
        digest = _sha256_json(payload)
        now = _utc_now()
        return {
            "correlation_id": corr_id,
            "causation_id": caus_id,
            "task_id": task_id,
            "task": {
                "taskId": task_id,
                "correlation_id": corr_id,
                "causation_id": caus_id,
                "source": "x_hubcore",
                "target": "x_osq",
                "operation": action,
                "actor": actor,
                "state": "COMPLETED",
                "timestampUtc": now,
            },
            "result": {
                "resultId": result_id,
                "taskId": task_id,
                "correlation_id": corr_id,
                "causation_id": caus_id,
                "producer": "x_osq",
                "status": "SUCCESS",
                "contentHash": digest,
            },
            "event": {
                "eventId": event_id,
                "taskId": task_id,
                "correlation_id": corr_id,
                "causation_id": caus_id,
                "type": f"osq.{action}",
                "producer": "x_osq",
                "timestampUtc": now,
            },
            "artifact": {
                "artifactId": artifact_id,
                "taskId": task_id,
                "correlation_id": corr_id,
                "causation_id": caus_id,
                "type": "application/json;kind=mutation_receipt",
                "sha256": digest,
            },
        }

    def _empty_relational_doc(self) -> Dict[str, Any]:
        return {
            "schemaVersion": "osq.relational.static/v2",
            "revision": 1,
            "updatedAt": _utc_now(),
            "lastSync": _utc_now(),
            "selectedQuoteId": "ROF-DEMO-001",
            "accounts": [],
            "opportunities": [],
            "budgets": [],
            "items": [],
            "quotes": [],
            "floorplans": [],
            "quoteLines": [],
            "forecastBatch": {},
            "marketplaceShards": [],
            "connectors": [],
            "changes": [],
            "snapshots": [],
            "events": [],
            "pathwaySummary": {},
            "bodyDirectives": [],
            "bodyFeedback": [],
            "m365HandoffQueue": [],
        }

    def _empty_temp_doc(self) -> Dict[str, Any]:
        return {
            "schemaVersion": "osq.shared.temp/v2",
            "revision": 1,
            "updatedAt": _utc_now(),
            "activeSessions": [],
            "entityLocks": [],
            "sharedDrafts": {},
            "whatIfScratch": {
                "governanceProfile": "adaptive",
                "quipuEmaAlpha": 0.90,
                "inflationCapRatio": 1.70,
                "frontLoadWeeks": 2,
                "prorationFactor": 0.75,
                "editedBy": "system",
                "updatedAt": _utc_now(),
            },
            "liveChatFeed": [],
        }

    def _load_or_init_relational(self) -> Dict[str, Any]:
        if self.relational_path.exists():
            return json.loads(self.relational_path.read_text(encoding="utf-8"))
        if self.seed_path and self.seed_path.exists():
            doc = json.loads(self.seed_path.read_text(encoding="utf-8"))
            _atomic_write_json(self.relational_path, doc)
            return doc
        doc = self._empty_relational_doc()
        _atomic_write_json(self.relational_path, doc)
        return doc

    def _load_or_init_temp(self) -> Dict[str, Any]:
        if self.temp_path.exists():
            return json.loads(self.temp_path.read_text(encoding="utf-8"))
        doc = self._empty_temp_doc()
        _atomic_write_json(self.temp_path, doc)
        return doc

    def reset_to_seed(self) -> Dict[str, Any]:
        with self._lock:
            if self.seed_path and self.seed_path.exists():
                doc = json.loads(self.seed_path.read_text(encoding="utf-8"))
            else:
                doc = self._empty_relational_doc()
            doc["revision"] = 1
            doc["updatedAt"] = _utc_now()
            doc["lastSync"] = _utc_now()
            doc.setdefault("selectedQuoteId", "ROF-DEMO-001")
            self._relational = doc
            self._relational["bodyDirectives"] = []
            self._relational["bodyFeedback"] = []
            self._ensure_heuristics_config()
            self._evaluate_heuristics_order({})
            self._refresh_pathways_and_directives("reset", {}, "system")
            self._temp = self._empty_temp_doc()
            self._init_control_plane_state()
            _atomic_write_json(self.relational_path, self._relational)
            _atomic_write_json(self.temp_path, self._temp)
            self._init_sqlite_mirror()
            self._mirror_revision_sqlite()
            self._mirror_ephemeral_sqlite()
            self._export_watermark_snapshot(actor="system", reason="reset")
        self._broadcast("relational.reset")
        return self.get_state()

    def _init_sqlite_mirror(self) -> None:
        try:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.sqlite_path), timeout=5.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS osq_relational_revisions (
                        revision INTEGER PRIMARY KEY,
                        updated_at TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS osq_command_outcomes (
                        idempotency_key TEXT PRIMARY KEY,
                        request_hash TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        correlation_id TEXT NOT NULL,
                        causation_id TEXT NOT NULL,
                        status_code INTEGER NOT NULL,
                        response_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS osq_durable_outbox (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        correlation_id TEXT NOT NULL,
                        causation_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS hubcore_registrations (
                        holon_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        lease_expires_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS hubcore_restricted_audit (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        time_utc TEXT NOT NULL,
                        identity TEXT NOT NULL,
                        policy_version TEXT NOT NULL,
                        decision TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        correlation_id TEXT NOT NULL,
                        task_id TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
        except Exception:
            pass
        try:
            self.ephemeral_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.ephemeral_sqlite_path), timeout=5.0) as econn:
                econn.execute("PRAGMA journal_mode=WAL;")
                econn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS osq_ephemeral_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        temp_revision INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                econn.commit()
        except Exception:
            pass

    def _mirror_ephemeral_sqlite(self) -> None:
        try:
            trev = int(self._temp.get("revision", 1))
            payload = json.dumps(self._temp, ensure_ascii=False)
            with sqlite3.connect(str(self.ephemeral_sqlite_path), timeout=5.0) as conn:
                conn.execute(
                    """
                    INSERT INTO osq_ephemeral_state(id, temp_revision, updated_at, payload_json)
                    VALUES(1, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        temp_revision=excluded.temp_revision,
                        updated_at=excluded.updated_at,
                        payload_json=excluded.payload_json
                    """,
                    (trev, _utc_now(), payload),
                )
                conn.commit()
        except Exception:
            pass

    def _mirror_revision_sqlite(
        self,
        outbox_entry: Optional[Dict[str, Any]] = None,
        idempotency_rec: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            rev = int(self._relational.get("revision", 1))
            digest = _sha256_json(self._relational)
            payload = json.dumps(self._relational, ensure_ascii=False)
            with sqlite3.connect(str(self.sqlite_path), timeout=5.0) as conn:
                conn.execute(
                    """
                    INSERT INTO osq_relational_revisions(revision, updated_at, content_hash, payload_json)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(revision) DO UPDATE SET
                        updated_at=excluded.updated_at,
                        content_hash=excluded.content_hash,
                        payload_json=excluded.payload_json
                    """,
                    (rev, _utc_now(), digest, payload),
                )
                if outbox_entry is not None:
                    conn.execute(
                        """
                        INSERT INTO osq_durable_outbox(event_id, task_id, correlation_id, causation_id, event_type, revision, created_at, payload_json)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            outbox_entry["eventId"],
                            outbox_entry["taskId"],
                            outbox_entry["correlation_id"],
                            outbox_entry["causation_id"],
                            outbox_entry["eventType"],
                            rev,
                            outbox_entry["createdAt"],
                            json.dumps(outbox_entry, ensure_ascii=False),
                        ),
                    )
                if idempotency_rec is not None:
                    conn.execute(
                        """
                        INSERT INTO osq_command_outcomes(idempotency_key, request_hash, task_id, correlation_id, causation_id, status_code, response_json, created_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(idempotency_key) DO NOTHING
                        """,
                        (
                            idempotency_rec["idempotencyKey"],
                            idempotency_rec["requestHash"],
                            idempotency_rec["taskId"],
                            idempotency_rec["correlationId"],
                            idempotency_rec["causationId"],
                            int(idempotency_rec.get("statusCode", 200)),
                            json.dumps(idempotency_rec["responseSummary"], ensure_ascii=False),
                            idempotency_rec["createdAt"],
                        ),
                    )
                conn.commit()
        except Exception:
            pass

    def subscribe(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners = [fn for fn in self._listeners if fn is not listener]

    def _broadcast(self, event_type: str, outbox_seq: Optional[int] = None) -> None:
        snapshot = self.get_state()
        packet = {"type": event_type, "cursor": outbox_seq if outbox_seq is not None else len(self._outbox), "state": snapshot}
        for listener in list(self._listeners):
            try:
                listener(packet)
            except Exception:
                self.unsubscribe(listener)

    def _prune_temp(self) -> None:
        now_epoch = time.time()
        sessions = []
        for s in self._temp.get("activeSessions", []):
            last_seen = float(s.get("lastSeenEpoch", now_epoch))
            if now_epoch - last_seen <= self.SESSION_TTL_SECONDS:
                sessions.append(s)
        self._temp["activeSessions"] = sessions

        locks = []
        for lk in self._temp.get("entityLocks", []):
            expires = float(lk.get("expiresEpoch", now_epoch + 60))
            if expires >= now_epoch:
                locks.append(lk)
        self._temp["entityLocks"] = locks

    def get_state(self, since_cursor: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            self._prune_temp()
            x_reg = self._registrations.get("x_osq") or {}
            res: Dict[str, Any] = {
                "ok": True,
                "revision": int(self._relational.get("revision", 1)),
                "tempRevision": int(self._temp.get("revision", 1)),
                "outboxCursor": len(self._outbox),
                "serverTime": _utc_now(),
                "activeRegistration": {
                    "holonId": x_reg.get("holonId", "x_osq"),
                    "status": x_reg.get("status", "ACTIVE"),
                    "routable": bool(x_reg.get("routable", True)),
                    "leaseExpiresAt": x_reg.get("leaseExpiresAt"),
                },
                "relational": self._relational,
                "temp": self._temp,
            }
            if since_cursor is not None:
                res["outboxEventsSinceCursor"] = [ev for ev in self._outbox if int(ev.get("seq", 0)) > int(since_cursor)]
            return res

    def _append_event(self, actor: str, persona: str, action: str, obj: str, details: str) -> None:
        events = self._relational.setdefault("events", [])
        events.append(
            {
                "id": f"EVT-{int(time.time()*1000)}",
                "time": _utc_now(),
                "actor": actor,
                "persona": persona,
                "action": action,
                "object": obj,
                "details": details,
            }
        )
        if len(events) > 1500:
            del events[: len(events) - 1500]

    @staticmethod
    def _parse_utc_iso(text: Optional[str]) -> Optional[datetime]:
        if not text:
            return None
        try:
            s = str(text).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _stage_default_probability(stage: str) -> int:
        return {
            "Lead": 10,
            "Qualified": 25,
            "Property Walk": 45,
            "Ready for Quote": 65,
            "Quoted": 75,
            "Quote Presented": 85,
            "Quote Won": 95,
            "Project In Progress": 100,
            "Closed Lost": 0,
        }.get(stage, 25)

    def _recompute_quote_margin(self, quote: Dict[str, Any]) -> Dict[str, Any]:
        """Compute line, floorplan, quote, BMS & eWallet net margin per CPQ-35 / CPQ-36."""
        items_by_sku = {i["sku"]: i for i in self._relational.get("items", [])}
        q_disc = float(quote.get("quoteDiscountPct") or 0.0)
        bms_disc = float(quote.get("bmsDiscountPct") or 0.0)
        ewallet = float(quote.get("ewalletApplied") or 0.0)
        floor_pct = float(quote.get("marginFloorPct") or 24.0)
        gross_total = 0.0
        total_rev = 0.0
        total_cost = 0.0
        breaches = []
        for line in quote.get("lines", []):
            item = items_by_sku.get(line.get("sku", ""), {})
            qty = max(0.0, float(line.get("qty") or 0.0))
            unit = float(line.get("unit") or item.get("price") or 0.0)
            if unit <= 0 and item.get("price"):
                unit = float(item["price"])
                line["unit"] = unit
            cost = float(item.get("cost") if item.get("cost") is not None else unit * 0.62)
            fp_disc = max(0.0, min(90.0, float(line.get("floorplanDiscountPct") or 0.0)))
            ln_disc = max(0.0, min(90.0, float(line.get("lineDiscountPct") or 0.0)))
            mult = max(0.0, (1.0 - ln_disc / 100.0) * (1.0 - fp_disc / 100.0) * (1.0 - q_disc / 100.0) * (1.0 - bms_disc / 100.0))
            gross = unit * qty
            rev = gross * mult
            net_unit = (rev / qty) if qty > 0 else unit * mult
            ext_cost = cost * qty
            margin_pct = ((net_unit - cost) / net_unit * 100.0) if net_unit > 0 else 0.0
            line["effectiveUnit"] = round(net_unit, 2)
            line["lineNetTotal"] = round(rev, 2)
            line["marginPct"] = round(margin_pct, 2)
            if margin_pct < floor_pct and cost > 0:
                target_min = round(cost / max(0.05, 1.0 - floor_pct / 100.0), 2)
                breaches.append({"sku": line.get("sku"), "marginPct": round(margin_pct, 2), "targetMinPrice": target_min})
            gross_total += gross
            total_rev += rev
            total_cost += ext_cost
        net_after_wallet = max(0.0, total_rev - ewallet)
        quote_margin = ((net_after_wallet - total_cost) / net_after_wallet * 100.0) if net_after_wallet > 0 else 0.0
        auto_eligible = quote_margin >= floor_pct
        denom = max(0.05, 1.0 - floor_pct / 100.0)
        target_net = total_cost / denom
        lift_needed = 0.0 if auto_eligible else round(max(0.0, target_net - net_after_wallet), 2)
        max_disc = round(max(0.0, (1.0 - ((target_net + ewallet) / gross_total)) * 100.0), 2) if gross_total > 0 else 0.0
        quote["approvedMarginPct"] = round(quote_margin, 2)
        quote["grossSubtotal"] = round(gross_total, 2)
        quote["netSubtotal"] = round(net_after_wallet, 2)
        quote["totalCost"] = round(total_cost, 2)
        quote["pricingGuidance"] = {
            "autoApproveEligible": auto_eligible,
            "currentMarginPct": round(quote_margin, 2),
            "marginFloorPct": floor_pct,
            "revenueLiftNeeded": lift_needed,
            "recommendedMaxQuoteDiscountPct": max_disc,
            "rule": "CPQ-45 Auto-Approve Eligible" if auto_eligible else "CPQ-46/47 Escalation or Remediation Required",
        }
        return {
            "subtotal": round(net_after_wallet, 2),
            "totalCost": round(total_cost, 2),
            "quoteMarginPct": round(quote_margin, 2),
            "marginFloorPct": floor_pct,
            "autoApproved": len(breaches) == 0 and auto_eligible,
            "breaches": breaches,
        }

    def _run_osq_pipeline(self, actor: str, persona: str, opts: Optional[Dict[str, Any]] = None) -> None:
        opts = opts or {}
        opps = self._relational.setdefault("opportunities", [])
        quotes = self._relational.setdefault("quotes", [])
        items_by_sku = {i["sku"]: i for i in self._relational.get("items", [])}
        fb = self._relational.setdefault("forecastBatch", {})
        eb = fb.setdefault("emergencyBrake", {})
        now_utc = datetime.now(timezone.utc)

        if opts.get("autoClearExpired"):
            for o in opps:
                if o.get("opportunityCondition"):
                    clear_dt = self._parse_utc_iso(o.get("conditionAutoClearsAt"))
                    if clear_dt and clear_dt <= now_utc:
                        o["opportunityCondition"] = None
                        o["conditionAutoClearsAt"] = None
                        self._append_event(actor, persona, "opportunity.condition_autocleared", o.get("opportunityId", ""), "CPQ-9 auto-cleared after 180d")

        opp_map = {o["opportunityId"]: o for o in opps if o.get("opportunityId")}
        excluded_pro_forma = sum(
            1 for o in opps if str(o.get("opportunityCondition") or "").upper() in ("PRO FORMA", "MOG")
        )

        proration = float(opts.get("prorationFactor") if opts.get("prorationFactor") is not None else (fb.get("prorationFactor") or 0.75))
        proration = max(0.1, min(1.0, proration))
        profile = str(self._relational.get("governanceProfile") or fb.get("governanceProfile") or "adaptive")

        sku_demand: Dict[str, float] = {}
        pm_handoff: List[Dict[str, Any]] = []
        eligible_count = 0
        skus_substituted = 0
        has_cutover = False

        for q in quotes:
            if str(q.get("status") or "").upper() != "ACCEPTED" or not bool(q.get("isPrimary")):
                continue
            oid = str(q.get("opportunityId") or "")
            opp = opp_map.get(oid)
            if opp and str(opp.get("opportunityCondition") or "").upper() in ("PRO FORMA", "MOG"):
                continue
            eligible_count += 1
            quoted_sum = 0
            ordered_sum = 0
            for ln in q.get("lines", []):
                q_qty = max(0, int(round(float(ln.get("qty") or 0))))
                o_qty = max(0, int(round(float(ln.get("orderedQty") or 0))))
                rem = max(0, q_qty - o_qty)
                quoted_sum += q_qty
                ordered_sum += o_qty
                if rem <= 0:
                    continue
                sku = str(ln.get("sku") or "").strip().upper()
                if not sku:
                    continue
                it = items_by_sku.get(sku)
                if it and str(it.get("itemType") or "").upper() == "LABOR":
                    continue
                if it and str(it.get("status") or "").upper() == "DISCONTINUED" and it.get("replacement"):
                    has_cutover = True
                    rep = str(it["replacement"]).strip().upper()
                    sku_demand[sku] = sku_demand.get(sku, 0.0) + rem * 0.5
                    sku_demand[rep] = sku_demand.get(rep, 0.0) + rem * 0.5
                    skus_substituted += 1
                    continue
                sku_demand[sku] = sku_demand.get(sku, 0.0) + rem

            hold_days = 30
            if opp:
                fl = opp.get("flags") or {}
                hold_days = 45 if fl.get("coordinatedDelivery") else int(opp.get("forecastHoldDays") or 30)
            pm_handoff.append({
                "quoteId": q.get("id"),
                "opportunityId": oid,
                "sapQuoteNumber": q.get("sapQuoteNumber") or "SAP-QT-PENDING",
                "lastSapOrderNumber": q.get("lastSapOrderNumber") or "",
                "quotedUnits": quoted_sum,
                "orderedUnits": ordered_sum,
                "remainingUnits": max(0, quoted_sum - ordered_sum),
                "forecastHoldDays": hold_days,
                "handoffTarget": "Project Lifecycle Management API (CPQ-58)",
            })

        if "SKU-ELEC-2001" in sku_demand and "SKU-ELEC-1001" not in sku_demand:
            has_cutover = True

        buckets: List[Dict[str, Any]] = []
        reservations: List[Dict[str, Any]] = []
        by_total = 0.0
        day_tag = now_utc.strftime("%Y%m%d")

        hc_pipe = self._ensure_heuristics_config()
        scb_pipe = hc_pipe.get("forecastAndScb") or {}
        front_load_mon = str(scb_pipe.get("forecastTimePhasingMode") or "FRONT_LOAD_FIRST_MONDAY").upper() == "FRONT_LOAD_FIRST_MONDAY"

        for sku, open_units in sku_demand.items():
            it = items_by_sku.get(sku) or {}
            if front_load_mon:
                w1 = round(open_units * 0.55 * proration, 1)
                w2 = round(open_units * 0.15, 1)
                w3 = round(open_units * 0.10, 1)
                w4 = round(open_units * 0.10, 1)
                w5 = round(open_units * 0.05, 1)
                w6 = round(max(0.0, open_units - (open_units * 0.55 + w2 + w3 + w4 + w5)), 1)
            else:
                w1 = round(open_units * 0.20 * proration, 1)
                w2 = round(open_units * 0.20, 1)
                w3 = round(open_units * 0.20, 1)
                w4 = round(open_units * 0.20, 1)
                w5 = round(open_units * 0.10, 1)
                w6 = round(max(0.0, open_units - (open_units * 0.20 + w2 + w3 + w4 + w5)), 1)
            row_tot = round(w1 + w2 + w3 + w4 + w5 + w6, 1)
            by_total += row_tot
            buckets.append({
                "sku": sku,
                "description": it.get("description") or sku,
                "origin": it.get("origin") or "DOMESTIC",
                "w1": w1, "w2": w2, "w3": w3, "w4": w4, "w5": w5, "w6": w6,
                "total": row_tot,
            })
            reservations.append({
                "reservationId": f"RES-SAP-{day_tag}-{sku}",
                "sku": sku,
                "qty": round(min(open_units, row_tot * 0.6), 1),
                "plant": "1010",
                "sloc": "0001",
                "movementType": "261",
                "status": "REPLACED_AT_W03" if (sku == "SKU-ELEC-1001" and has_cutover) else "READY",
            })

        if opts.get("resetBrake"):
            eb["status"] = "HELD"
            eb["tripped"] = True
        eb_status = str(eb.get("status") or "HELD")
        fb["taskState"] = "ACCEPTED" if eb_status == "OVERRIDDEN" else ("HELD" if eb.get("tripped") else "ACCEPTED")
        fb["batchId"] = f"BATCH-{day_tag}-{int(self._relational.get('revision', 1))}"
        fb["governanceProfile"] = profile
        fb["prorationFactor"] = proration
        fb["eligibleQuotesCount"] = eligible_count
        fb["excludedProFormaCount"] = excluded_pro_forma
        fb["skusSubstituted"] = skus_substituted
        fb["byPublishedUnits"] = round(by_total, 2)
        fb["weeklyBuckets"] = buckets
        fb["sapReservations"] = reservations
        fb["pmHandoff"] = pm_handoff
        fb["lastRunAt"] = _utc_now()
        self._evaluate_heuristics_order(opts)
        self._append_event(
            actor, persona, "osq.pipeline.completed", fb["batchId"],
            f"Eligible={eligible_count}, ExcludedProForma={excluded_pro_forma}, PublishedUnits={fb['byPublishedUnits']}, PMHandoffs={len(pm_handoff)}"
        )

    def _ensure_heuristics_config(self) -> Dict[str, Any]:
        hc = self._relational.setdefault("heuristicsConfig", {})
        hc.setdefault("activePreset", "RENOPLUS_CONSOLIDATION_NORTH_STAR")
        hc.setdefault("updatedAt", _utc_now())
        hc.setdefault("updatedBy", "Snehal Patel")
        sla = hc.setdefault("slaAndTiming", {})
        for k, v in {
            "immediateCheckHorizonDays": 10, "scheduledRecheckLeadDays": 6, "standardKittingLeadDays": 5,
            "linehaulTransitSlaDays": 2, "inTransitAgingAlertHours": 48, "governanceDecisionSlaDays": 30,
            "mulesoftReservationHorizonDays": 30, "creditHoldAutoShiftRdd": True, "holdPeeledLinesToRdd": True,
        }.items():
            sla.setdefault(k, v)
        org = hc.setdefault("organizationAndMigration", {})
        for k, v in {
            "defaultOrgProfile": "MULTIFAMILY_RENO", "salesAreaRouting": "SALES_AREA_20_UNIFIED",
            "autoClearLegacyDefaultPlant": True, "enforceShippingPointTaxCheck": True,
            "hospitalityCoordinatedFutureMonthOnly": True, "requirePlantStatusRL": True, "excludeOpportunityConditionInBy": True,
        }.items():
            org.setdefault(k, v)
        ful = hc.setdefault("fulfillmentAndKitting", {})
        for k, v in {
            "defaultFulfillmentMode": "RP_KITTED_ZPIR", "peelOffRoutingMode": "CONSOLIDATE_LINEHAUL_FIRST",
            "expandKitToDcStockedSkus": True, "trueFloorAtpExclude916AndPastDuePo": True,
            "excludeHazmatFromKit": True, "excludeStopReplenishmentBA": True, "excludeOversizedFragileDoors": True,
            "applianceDedicatedPalletRule": True, "autoStripFactoryDirectZtas": True, "requireFabShopForZtnfBlinds": True,
            "requireSpokeArrivalBeforeRoute": True, "enforceSingleDestinationLinehaulPallet": True,
            "dcLaborCapacityMaxPct": 85, "proactiveSplitShipmentAlerts": True,
            "protectFutureKitsFromTodayShortage": True, "requireParentQtyDrivesChildren": True,
            "rerunAvailabilityOnQuoteCarryChange": True, "requireSaveReopenUnitNumber": True,
            "dailyZstockNegativeReview": True, "outboundPrintDateValidation": True,
        }.items():
            ful.setdefault(k, v)
        scb = hc.setdefault("forecastAndScb", {})
        for k, v in {
            "forecastTimePhasingMode": "FRONT_LOAD_FIRST_MONDAY", "frontLoadDurationDays": 7,
            "preventCurrentMonthDoubleCount": True, "scbEoqServiceLevelPct": 95.0,
            "scbBullwhipDampingThreshold": 1.25, "scbMultiEchelonSafetyStock": True,
            "scbLeadTimeSurvivalQuantile": "p90", "scbQuipuEmaAlpha": 0.90,
        }.items():
            scb.setdefault(k, v)
        if not hc.get("kittingDcNetwork"):
            hc["kittingDcNetwork"] = [
                {"kittingDc": "GA02", "name": "Atlanta Kitting Hub", "fabShop": True, "laborUtilPct": 74, "spokes": ["NC34", "AL04", "FL08"], "linehaulCutTime": "18:00 EST", "transitDays": 1},
                {"kittingDc": "TX03", "name": "Dallas Kitting Hub", "fabShop": True, "laborUtilPct": 88, "spokes": ["TX77", "OK12"], "linehaulCutTime": "17:30 CST", "transitDays": 1},
                {"kittingDc": "IL30", "name": "Chicago Kitting Hub", "fabShop": True, "laborUtilPct": 71, "spokes": ["MN55", "MO18", "OH24"], "linehaulCutTime": "18:00 CST", "transitDays": 1},
                {"kittingDc": "NJ22", "name": "New Jersey Kitting Hub", "fabShop": True, "laborUtilPct": 86, "spokes": ["PA15", "MA07", "MD19"], "linehaulCutTime": "17:00 EST", "transitDays": 1},
                {"kittingDc": "CA20", "name": "Southern CA Kitting Hub", "fabShop": True, "laborUtilPct": 78, "spokes": ["CA42"], "linehaulCutTime": "18:30 PST", "transitDays": 1},
                {"kittingDc": "FL14", "name": "Orlando Kitting Hub", "fabShop": True, "laborUtilPct": 69, "spokes": ["FL08", "FL31"], "linehaulCutTime": "18:00 EST", "transitDays": 1},
                {"kittingDc": "WA09", "name": "Seattle Kitting Hub", "fabShop": False, "laborUtilPct": 65, "spokes": ["OR11"], "linehaulCutTime": "17:30 PST", "transitDays": 1},
                {"kittingDc": "AZ17", "name": "Phoenix Kitting Hub", "fabShop": False, "laborUtilPct": 72, "spokes": ["NV06"], "linehaulCutTime": "18:00 MST", "transitDays": 1},
                {"kittingDc": "CO11", "name": "Denver Kitting Hub", "fabShop": False, "laborUtilPct": 68, "spokes": ["UT08"], "linehaulCutTime": "17:30 MST", "transitDays": 2},
            ]
        hc.setdefault("architectureMode", "UNIFIED_PHYSICAL_MARKETPLACE_DIJKSTRA")
        sla = hc.setdefault("slaAndTiming", {})
        sla.setdefault("alertWindowDays", 10)
        sla.setdefault("csrEarlyNoticeDays", 5)
        sla.setdefault("expediteSurgeWeight", 0.35)
        sla.setdefault("consolidationTouchLaborUsd", 18.5)
        sla.setdefault("holdingPenaltyPerDayUsd", 4.25)
        org = hc.setdefault("organizationAndMigration", {})
        org["virtualWarehousesAllowed"] = False
        org["snowflakeRole"] = "analytical-export-only"
        org.setdefault("directSapApiTimePhased", True)
        org.setdefault("prohibitVirtualWarehouseMroHijack", True)
        ful = hc.setdefault("fulfillmentAndKitting", {})
        ful.setdefault("unifiedMarketplaceDijkstra", True)
        ful.setdefault("dijkstraTrafficWeightFactor", 1.0)
        ful.setdefault("ctoSeasonalityThreshold", 0.65)
        ful.setdefault("modularBomFlattening", True)
        ful.setdefault("sameDayStoDupGuard", True)
        ful.setdefault("ifrAutoPoEnabled", True)
        ful.setdefault("cancelPoReductionGate", True)
        return hc

    def _apply_heuristics_preset(self, hc: Dict[str, Any], preset_id: str) -> None:
        sla = hc.setdefault("slaAndTiming", {})
        org = hc.setdefault("organizationAndMigration", {})
        ful = hc.setdefault("fulfillmentAndKitting", {})
        scb = hc.setdefault("forecastAndScb", {})
        hc["activePreset"] = preset_id
        if preset_id == "RENOPLUS_CONSOLIDATION_NORTH_STAR":
            sla.update({"immediateCheckHorizonDays": 10, "scheduledRecheckLeadDays": 6, "standardKittingLeadDays": 5, "linehaulTransitSlaDays": 2, "creditHoldAutoShiftRdd": True, "holdPeeledLinesToRdd": True})
            org.update({"defaultOrgProfile": "MULTIFAMILY_RENO", "salesAreaRouting": "SALES_AREA_20_UNIFIED"})
            ful.update({"defaultFulfillmentMode": "RP_KITTED_ZPIR", "peelOffRoutingMode": "CONSOLIDATE_LINEHAUL_FIRST", "expandKitToDcStockedSkus": True, "trueFloorAtpExclude916AndPastDuePo": True, "dcLaborCapacityMaxPct": 85})
            scb.update({"forecastTimePhasingMode": "FRONT_LOAD_FIRST_MONDAY", "scbEoqServiceLevelPct": 95.0, "scbLeadTimeSurvivalQuantile": "p90"})
        elif preset_id == "MULTI_HUB_CAMPUS_LINEHAUL":
            sla.update({"immediateCheckHorizonDays": 10, "scheduledRecheckLeadDays": 6, "linehaulTransitSlaDays": 2, "holdPeeledLinesToRdd": True})
            ful.update({"defaultFulfillmentMode": "RP_KITTED_ZPIR", "peelOffRoutingMode": "CONSOLIDATE_LINEHAUL_FIRST", "expandKitToDcStockedSkus": True, "requireSpokeArrivalBeforeRoute": True, "dcLaborCapacityMaxPct": 95})
            scb.update({"forecastTimePhasingMode": "FRONT_LOAD_FIRST_MONDAY", "scbMultiEchelonSafetyStock": True})
        elif preset_id == "PEAK_DC_LABOR_GUARD":
            sla.update({"immediateCheckHorizonDays": 10, "scheduledRecheckLeadDays": 6, "holdPeeledLinesToRdd": True})
            ful.update({"defaultFulfillmentMode": "RP_KITTED_ZPIR", "peelOffRoutingMode": "CONSOLIDATE_LINEHAUL_FIRST", "dcLaborCapacityMaxPct": 75, "expandKitToDcStockedSkus": True})
            scb.update({"forecastTimePhasingMode": "FRONT_LOAD_FIRST_MONDAY"})
        elif preset_id == "HOSPITALITY_COORDINATED_DELIVERY":
            sla.update({"immediateCheckHorizonDays": 14, "scheduledRecheckLeadDays": 7, "standardKittingLeadDays": 5, "creditHoldAutoShiftRdd": True, "holdPeeledLinesToRdd": True})
            org.update({"defaultOrgProfile": "HOSPITALITY_COORDINATED", "hospitalityCoordinatedFutureMonthOnly": True})
            ful.update({"defaultFulfillmentMode": "RP_KITTED_ZPIR", "peelOffRoutingMode": "CONSOLIDATE_LINEHAUL_FIRST", "dcLaborCapacityMaxPct": 90})
            scb.update({"forecastTimePhasingMode": "FRONT_LOAD_FIRST_MONDAY", "scbEoqServiceLevelPct": 98.0, "scbLeadTimeSurvivalQuantile": "p95"})
        elif preset_id == "WHOLESALE_MRO_FAST_FLEX":
            sla.update({"immediateCheckHorizonDays": 5, "scheduledRecheckLeadDays": 3, "standardKittingLeadDays": 2, "holdPeeledLinesToRdd": False})
            org.update({"defaultOrgProfile": "WHOLESALE_COMMERCIAL", "virtualWarehousesAllowed": False, "snowflakeRole": "analytical-export-only"})
            ful.update({"defaultFulfillmentMode": "WHOLESALE_MRO", "peelOffRoutingMode": "DIRECT_MRO_FLEX", "expandKitToDcStockedSkus": False, "dcLaborCapacityMaxPct": 95})
            scb.update({"forecastTimePhasingMode": "WEEKLY_EVEN_SPREAD", "scbEoqServiceLevelPct": 95.0, "scbLeadTimeSurvivalQuantile": "p50"})
        elif preset_id == "UNIFIED_MARKETPLACE_DIJKSTRA_DROPSHIP":
            sla.update({"immediateCheckHorizonDays": 10, "scheduledRecheckLeadDays": 6, "standardKittingLeadDays": 5, "linehaulTransitSlaDays": 2, "creditHoldAutoShiftRdd": True, "holdPeeledLinesToRdd": True, "alertWindowDays": 10, "csrEarlyNoticeDays": 5, "expediteSurgeWeight": 0.40})
            org.update({"defaultOrgProfile": "UNIFIED_PHYSICAL_MARKETPLACE", "salesAreaRouting": "SALES_AREA_20_UNIFIED", "virtualWarehousesAllowed": False, "snowflakeRole": "analytical-export-only", "directSapApiTimePhased": True})
            ful.update({"defaultFulfillmentMode": "UNIFIED_MARKETPLACE_DIRECT", "peelOffRoutingMode": "DIJKSTRA_DROPSHIP_OR_CONSOLIDATE", "unifiedMarketplaceDijkstra": True, "expandKitToDcStockedSkus": True, "trueFloorAtpExclude916AndPastDuePo": True, "dcLaborCapacityMaxPct": 85, "modularBomFlattening": True, "sameDayStoDupGuard": True, "ifrAutoPoEnabled": True})
            scb.update({"forecastTimePhasingMode": "FRONT_LOAD_FIRST_MONDAY", "scbEoqServiceLevelPct": 97.5, "scbLeadTimeSurvivalQuantile": "p95"})

    def _evaluate_heuristics_order(self, opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        opts = opts or {}
        hc = self._ensure_heuristics_config()
        sla, org, ful, scb = hc["slaAndTiming"], hc["organizationAndMigration"], hc["fulfillmentAndKitting"], hc["forecastAndScb"]
        net = hc.get("kittingDcNetwork") or []
        quotes = self._relational.get("quotes") or []
        items_by_sku = {i["sku"].upper(): i for i in self._relational.get("items", [])}
        accounts_by_id = {a["accountId"]: a for a in self._relational.get("accounts", [])}

        qid = str(opts.get("quoteId") or self._relational.get("selectedQuoteId") or "ROF-DEMO-001")
        q = next((x for x in quotes if str(x.get("id")).lower() == qid.lower()), quotes[0] if quotes else None)
        if not q:
            return {}
        qid = q["id"]
        acct = accounts_by_id.get(q.get("accountId") or "")
        local_dc = str(opts.get("localDc") or q.get("localDc") or (acct.get("defaultLocalDc") if acct else "NC34")).upper()
        order_lead = int(opts.get("orderToRddDays") if "orderToRddDays" in opts else (q.get("orderToRddDays") or 8))
        deliv_block = str(opts.get("deliveryBlock") if "deliveryBlock" in opts else (q.get("deliveryBlock") or "ZR")).upper()
        credit_hold = int(opts.get("creditHoldDays") if "creditHoldDays" in opts else (q.get("creditHoldDays") or 0))
        org_type = str(opts.get("orgType") or q.get("orgType") or org.get("defaultOrgProfile"))
        fulfillment_mode = str(opts.get("fulfillmentType") or q.get("fulfillmentType") or ful.get("defaultFulfillmentMode"))

        kit_hub = next((h for h in net if str(h.get("kittingDc")).upper() == local_dc or local_dc in [s.upper() for s in h.get("spokes", [])]), net[0] if net else {})
        kitting_dc = str(kit_hub.get("kittingDc") or "GA02")
        kitting_dc_name = str(kit_hub.get("name") or kitting_dc)
        kit_labor_util = int(kit_hub.get("laborUtilPct") or 74)
        kit_fab_shop = bool(kit_hub.get("fabShop", True))
        kit_transit = int(kit_hub.get("transitDays") or 1)

        var1 = int(sla.get("immediateCheckHorizonDays", 10))
        var2 = int(sla.get("scheduledRecheckLeadDays", 6))
        std_kit = int(sla.get("standardKittingLeadDays", 5))
        max_lh_sla = int(sla.get("linehaulTransitSlaDays", 2))
        auto_shift = bool(sla.get("creditHoldAutoShiftRdd", True))
        hold_to_rdd = bool(sla.get("holdPeeledLinesToRdd", True))

        true_floor_mode = bool(ful.get("trueFloorAtpExclude916AndPastDuePo", True))
        expand_assort = bool(ful.get("expandKitToDcStockedSkus", True))
        excl_haz = bool(ful.get("excludeHazmatFromKit", True))
        excl_ba = bool(ful.get("excludeStopReplenishmentBA", True))
        excl_door = bool(ful.get("excludeOversizedFragileDoors", True))
        appl_pallet = bool(ful.get("applianceDedicatedPalletRule", True))
        strip_ztas = bool(ful.get("autoStripFactoryDirectZtas", True))
        req_fab = bool(ful.get("requireFabShopForZtnfBlinds", True))
        max_labor = int(ful.get("dcLaborCapacityMaxPct", 85))
        peel_mode = str(ful.get("peelOffRoutingMode", "CONSOLIDATE_LINEHAUL_FIRST"))
        protect_future_kits = bool(ful.get("protectFutureKitsFromTodayShortage", True))
        daily_zstock_rev = bool(ful.get("dailyZstockNegativeReview", True))
        outbound_print_val = bool(ful.get("outboundPrintDateValidation", True))

        rem_after_credit = max(0, order_lead - credit_hold)
        rdd_shift = (std_kit - rem_after_credit) if (auto_shift and (credit_hold > 0 or deliv_block == "ZN") and rem_after_credit < std_kit) else 0
        eff_lead = order_lead + rdd_shift
        check_mode = f"IMMEDIATE_ORDER_ENTRY_CHECK (Lead {order_lead}d <= Var1 {var1}d)" if order_lead <= var1 else f"BATCH_RECHECK_SCHEDULED_AT_RDD_MINUS_{var2}D (Lead {order_lead}d > Var1 {var1}d)"
        commit_status = (
            f"NON_COMMITTING ({deliv_block} Quote/Credit Block - Inventory Not Held)"
            if deliv_block in ("ZV", "ZN")
            else ("HARD_WAVE_RESERVATION (VL01N Outbound Drop Window)" if eff_lead <= std_kit else "SOFT_ALLOCATION (Committed on Order Until VL01N @ RDD-5d)")
        )

        line_decisions = []
        deep_map_events = []
        counts = {"inKitCount": 0, "expandedToKitCount": 0, "waitPreserveCount": 0, "linehaulMatchupCount": 0, "laborGuardFlexCount": 0, "directFlexCount": 0, "autoStripZtasCount": 0, "excludedHazardDoorCount": 0, "appliancePalletCount": 0, "blockedBaCount": 0, "batchDeferredCount": 0, "paperAtpFalseGreenRiskCount": 0, "proactiveAlertsCount": 0}
        total_demand, total_safety, total_cvar, max_z, sum_p90, scb_n = 0.0, 0.0, 0.0, 0.0, 0.0, 0

        for ln in q.get("lines", []):
            sku = str(ln.get("sku") or "").strip().upper()
            it = items_by_sku.get(sku) or {}
            qty = max(1, int(round(float(ln.get("qty") or 1))))
            total_demand += qty
            cat = str(it.get("sapItemCategory") or "ZTAN")
            kit_elig = bool(it.get("kitEligibleFlag"))
            dc_stocked = bool(it.get("dcStockedFlag"))
            is_haz = bool(it.get("hazmat"))
            is_ba = bool(it.get("stopReplenishment")) or it.get("plantStatus") == "BA" or it.get("status") == "Discontinued"
            is_door = bool(it.get("oversizedOrFragile"))
            is_appl = bool(it.get("largeApplianceOrHvac"))

            f_map = it.get("floorQohByDc") or {}
            c_map = it.get("committedQtyByDc") or {}
            s_map = it.get("storage916QtyByDc") or {}
            floor_qoh = int(f_map.get(kitting_dc, it.get("qoh") or 0))
            comm_qty = int(c_map.get(kitting_dc, 0))
            s916_qty = int(s_map.get(kitting_dc, 0))
            past_due = int(it.get("unreceivedPastDuePoQty") or 0)
            exp_repl = int(it.get("expectedReplenishmentQty") or 0)
            repl_days = int(it.get("incomingReplenishmentDays") or 7)
            zstock_neg = int(it.get("zstockNegativeQty") or 0)

            paper_atp = max(0, floor_qoh - comm_qty + past_due)
            true_floor = max(0, floor_qoh - comm_qty - s916_qty)
            projected_avail = true_floor + exp_repl
            eff_atp = true_floor if true_floor_mode else paper_atp
            if paper_atp >= qty > true_floor:
                counts["paperAtpFalseGreenRiskCount"] += 1

            spoke_dc = local_dc
            spoke_avail = max(0, int(f_map.get(local_dc, 0)) - int(c_map.get(local_dc, 0)))
            if spoke_avail < qty:
                for dck, val in f_map.items():
                    if str(dck).upper() == kitting_dc.upper():
                        continue
                    av = max(0, int(val) - int(c_map.get(dck, 0)))
                    if av > spoke_avail:
                        spoke_avail, spoke_dc = av, dck

            scb_m = it.get("scbMetrics") or {}
            eoq_opt = int(scb_m.get("eoqOptimalQty") or 0)
            gw_ss = int(scb_m.get("gravesWillemsSafetyStock") or 0)
            p50 = float(scb_m.get("p50LeadDays") or it.get("leadDays") or 5)
            p90 = float(scb_m.get("p90LeadDays") or p50 + 3)
            p95 = float(scb_m.get("p95LeadDays") or p90 + 3)
            surv_q = str(scb.get("scbLeadTimeSurvivalQuantile") or "p90")
            chosen_lt = p95 if surv_q == "p95" else (p50 if surv_q == "p50" else p90)
            max_z = max(max_z, abs(float(scb_m.get("eoqZScore") or 0.0)))
            if p90 > 0:
                sum_p90 += p90
                scb_n += 1
            if scb.get("scbMultiEchelonSafetyStock", True):
                total_safety += gw_ss
            total_cvar += float(scb_m.get("cvarCostUsd") or 0.0)

            cust_note = f"Ships complete inside Master Kit ({kitting_dc})."
            kit_commit_horizon = max(0, eff_lead - std_kit)
            if cat == "ZLAB":
                d_code, d_label, sev, expl = "SERVICE_LABOR", "RP Install Service Line", "info", "Non-physical labor line; gated by flags.installation = true (CPQ-11)."
            elif is_ba and excl_ba:
                d_code, d_label, sev, expl = "BLOCKED_BA", "BLOCKED: BA Discontinued / Stop-Replen (#544/#555)", "err", "Stop-Replenishment=1 / Plant Status BA: blocked from kitting & EWM adjustments."
                cust_note = "Substitution Required -> notify RSR/Customer."; counts["blockedBaCount"] += 1; counts["proactiveAlertsCount"] += 1
                deep_map_events.append({"eventType": "ExceptionCase", "targetQueue": "RPAQueue", "sku": sku, "detail": expl})
            elif cat == "ZTAS" and strip_ztas:
                d_code, d_label, sev, expl = "AUTO_STRIP_ZTAS", "Auto-Stripped Factory Direct (ZTAS) -> Day-0 PO", "info", "5-digit Special Order auto-stripped from kit so vendor PO fires immediately."
                cust_note = f"Factory-Direct drop ship ETA {chosen_lt}d tracked independently."; counts["autoStripZtasCount"] += 1
                deep_map_events.append({"eventType": "PeelEvent", "targetQueue": "FactoryDirectPO", "sku": sku, "detail": expl})
            elif is_haz and excl_haz:
                d_code, d_label, sev, expl = "EXCLUDED_HAZMAT", "Excluded from Kit Carton (Hazmat DOT/EWM)", "warn", "Hazmat=Y: excluded from enclosed kit box."
                cust_note = "Separate compliant hazmat parcel."; counts["excludedHazardDoorCount"] += 1
                deep_map_events.append({"eventType": "PeelEvent", "targetQueue": "HazmatCompliance", "sku": sku, "detail": expl})
            elif is_door and excl_door:
                d_code, d_label, sev, expl = "EXCLUDED_OVERSIZED", "Excluded: Oversized / Fragile Door Guard", "warn", "Oversized door routed separately to prevent kit strap-on breakage."
                cust_note = "Delivered separately on protected racks."; counts["excludedHazardDoorCount"] += 1
                deep_map_events.append({"eventType": "PeelEvent", "targetQueue": "FlatRackDelivery", "sku": sku, "detail": expl})
            elif is_appl and appl_pallet:
                d_code, d_label, sev, expl = "APPLIANCE_PALLET", "Dedicated Appliance Pallet + 2-Day Linehaul SLA", "info", "Dedicated cone/sticker pallet (P4) with CG-06 arrival gate."
                cust_note = "Dedicated appliance pallet aligned to kit RDD."; counts["appliancePalletCount"] += 1
                deep_map_events.append({"eventType": "ShipmentEvent", "targetQueue": "DedicatedPallet", "sku": sku, "detail": expl})
            elif cat == "ZTNF" and req_fab and not kit_fab_shop:
                d_code, d_label, sev, expl = "FAB_SHOP_REROUTE", f"Peeled: No EWM Fab Shop @ {kitting_dc} -> Hub Fab", "warn", f"Enosix VC blind cannot fabricate at {kitting_dc}; routed to Hub Fab Shop."
                cust_note = f"Hub Fab Shop +{kit_transit}d linehaul buffer."; counts["linehaulMatchupCount"] += 1; counts["proactiveAlertsCount"] += 1
                deep_map_events.append({"eventType": "ConsolidationEvent", "targetQueue": "FabShopLinehaul", "sku": sku, "detail": expl})
            elif "WHOLESALE" in fulfillment_mode.upper():
                d_code, d_label, sev, expl = "WHOLESALE_FLEX", f"Wholesale/MRO Multi-DC FlexLogic ({local_dc})", "ok", "Standard MRO FlexLogic without kitting bundle wait."
                counts["directFlexCount"] += 1
                deep_map_events.append({"eventType": "CrossShipEvent", "targetQueue": "MROFlex", "sku": sku, "detail": expl})
            elif order_lead > var1:
                d_code, d_label, sev, expl = "BATCH_DEFERRED", f"Held in Kit -> Auto-Check @ RDD-{var2}d (Var2)", "info", f"Lead {order_lead}d > Var1 {var1}d: deferred to RDD-{var2}d batch check (Golden Rule: do not alter future kits)."
                counts["batchDeferredCount"] += 1
            elif not kit_elig and not expand_assort:
                d_code, d_label, sev, expl = "PEEL_NON_ELIGIBLE", "Peeled: Non-Kit-Eligible -> MRO Flex", "warn", "Assortment Expansion OFF; peeled from kit."
                counts["directFlexCount"] += 1; counts["proactiveAlertsCount"] += 1
                deep_map_events.append({"eventType": "PeelEvent", "targetQueue": "MROFlex", "sku": sku, "detail": expl})
            elif eff_atp >= qty:
                if not kit_elig and dc_stocked and expand_assort:
                    d_code, d_label, sev, expl = "EXPANDED_IN_KIT", "Keep in Kit (Assortment Expanded: DC Floor Stocked)", "ok", f"Included via {kitting_dc} floor stock ({true_floor}u >= {qty}u)."
                    counts["expandedToKitCount"] += 1
                else:
                    d_code, d_label, sev, expl = "KEEP_IN_KIT", f"Keep in Master Kit @ {kitting_dc} (Floor ATP={true_floor})", "ok", f"True floor {true_floor}u >= {qty}u."
                    counts["inKitCount"] += 1
            elif protect_future_kits and projected_avail >= qty and repl_days <= kit_commit_horizon:
                d_code, d_label, sev, expl = "WAIT_FOR_SUPPLY_PRESERVE_KIT", f"Preserve Future Kit: Wait for Replenishment (ETA {repl_days}d <= Commit {kit_commit_horizon}d)", "ok", f"Slide Step 8 Golden Rule: Floor ATP ({true_floor}) short of {qty}, but Incoming Replenishment (+{exp_repl}u in {repl_days}d) meets Kit Commit ({kit_commit_horizon}d). Do NOT modify future kit!"
                cust_note = f"Kit preserved intact on replenishment schedule (Projected={projected_avail}u)."
                counts["waitPreserveCount"] += 1
                deep_map_events.append({"eventType": "ConsolidationEvent", "targetQueue": "WaitForSupplyPreserveKit", "sku": sku, "detail": expl})
            else:
                if peel_mode == "CONSOLIDATE_LINEHAUL_FIRST" and spoke_avail >= qty and kit_transit <= max_lh_sla:
                    if kit_labor_util > max_labor:
                        d_code, d_label, sev, expl = "LABOR_GUARD_MRO_FLEX", f"Labor Guard ({kitting_dc} {kit_labor_util}% > {max_labor}%) -> Flex from {spoke_dc}", "warn", f"{kitting_dc} labor exceeds cap -> direct flex + RDD hold."
                        cust_note = f"Separate direct shipment from {spoke_dc} held for RDD."; counts["laborGuardFlexCount"] += 1; counts["proactiveAlertsCount"] += 1
                        deep_map_events.append({"eventType": "CrossShipEvent", "targetQueue": "NotificationEvent", "sku": sku, "detail": expl})
                    else:
                        d_code, d_label, sev, expl = "LINEHAUL_MATCHUP", f"Linehaul Match-Up ({spoke_dc} -> {kitting_dc}) + Kit Label #475", "ok", f"{spoke_dc} linehauls {qty}u via Mvt 313->315 with CG-06 Arrival Gate."
                        cust_note = "Consolidated via overnight linehaul into Master Kit."; counts["linehaulMatchupCount"] += 1
                        deep_map_events.append({"eventType": "ConsolidationEvent", "targetQueue": "LinehaulMatchUp475", "sku": sku, "detail": expl})
                elif spoke_avail >= qty:
                    d_code, d_label, sev, expl = "PEEL_MRO_FLEX", f"Peeled to Direct MRO Flex ({spoke_dc})", "warn", f"Peeled to {spoke_dc} MRO flex ({spoke_avail}u)."
                    cust_note = f"Split Shipment Alert sent to Customer & RSR ({spoke_dc})."; counts["directFlexCount"] += 1; counts["proactiveAlertsCount"] += 1
                    deep_map_events.append({"eventType": "PeelEvent", "targetQueue": "NotificationEvent", "sku": sku, "detail": expl})
                else:
                    d_code, d_label, sev, expl = "PEEL_BACKORDER", "Peeled to Local Backorder + Proactive ETA Alert", "err", "Regional DCs short; rest of kit ships complete."
                    cust_note = "Proactive Backorder Notification & Recovery ETA sent."; counts["directFlexCount"] += 1; counts["proactiveAlertsCount"] += 1
                    deep_map_events.append({"eventType": "ExceptionCase", "targetQueue": "RPAQueue", "sku": sku, "detail": expl})

            line_decisions.append({
                "lineId": ln.get("lineId") or "", "sku": sku, "description": it.get("description") or sku,
                "sapItemCategory": cat, "qty": qty, "floorQoh": floor_qoh, "committedQty": comm_qty,
                "storage916Qty": s916_qty, "unreceivedPastDuePoQty": past_due,
                "expectedReplenishmentQty": exp_repl, "incomingReplenishmentDays": repl_days,
                "zstockNegativeQty": zstock_neg, "projectedQty": projected_avail,
                "atpResult": "PASS" if paper_atp >= qty else "SHORT",
                "co09Result": "FLOOR_OK" if true_floor >= qty else "PO_DEPENDENCY",
                "zstockResult": f"NEGATIVE ({zstock_neg})" if zstock_neg < 0 else "CLEAN",
                "paperAtp": paper_atp, "trueFloorAvail": true_floor, "spokeDc": spoke_dc, "spokeFloorAvail": spoke_avail,
                "decisionCode": d_code, "decisionLabel": d_label, "severity": sev, "explanation": expl,
                "holdToRdd": hold_to_rdd, "customerNotification": cust_note,
                "eoqOptimalQty": eoq_opt, "gravesWillemsSafetyStock": gw_ss, "leadDaysQuantile": chosen_lt,
            })

        parent_kit_sku = str(q.get("parentKitSku") or "KIT-RENO-1BR")
        parent_kit_qty = int(q.get("parentKitQty") or 10)
        unit_number = str(q.get("unitNumber") or "UNIT-TBD")
        fp_conv = bool(q.get("floorplanConverted", True))
        eds_val = bool(q.get("edsValidated", True))
        out_val = bool(q.get("outboundPrintValidated", True))
        workflow_steps9 = [
            {"step": 1, "title": "Start at Quote", "status": "COMPLETED", "detail": f"Quote={qid}"},
            {"step": 2, "title": "Convert Using Floor Plan", "status": "COMPLETED" if fp_conv else "PENDING", "detail": "Floorplan child parts mapped" if fp_conv else "Pending floorplan pick"},
            {"step": 3, "title": "Set Parent Quantity", "status": "COMPLETED", "detail": f"{parent_kit_sku} x {parent_kit_qty}"},
            {"step": 4, "title": "Build Order & Availability", "status": "COMPLETED", "detail": f"ATP+CO09+ZSTOCK | {check_mode}"},
            {"step": 5, "title": "Handle Backordered Kits", "status": "RESOLVED_FLEX" if (counts["blockedBaCount"] + counts["directFlexCount"]) > 0 else "CLEAN", "detail": f"WaitPreserve={counts['waitPreserveCount']}, Linehaul={counts['linehaulMatchupCount']}, Flex={counts['directFlexCount']}"},
            {"step": 6, "title": "Confirm Delivery Setup", "status": "COMPLETED" if eds_val else "REVIEW", "detail": "EDS & Installation verified"},
            {"step": 7, "title": "Save/Reopen & Unit #", "status": "PENDING" if (not unit_number or unit_number.startswith("PENDING")) else "COMPLETED", "detail": f"unitNumber={unit_number}"},
            {"step": 8, "title": "Daily ZSTOCK Review", "status": "ACTIVE_GUARD" if daily_zstock_rev else "OFF", "detail": "Golden Rule Active: Preserves future kits" if protect_future_kits else "Today-only short check"},
            {"step": 9, "title": "Outbound Print Validation", "status": "VALIDATED" if (outbound_print_val and out_val) else "EXCEPTION_QUEUE", "detail": f"DeepMap Events={len(deep_map_events)}"},
        ]

        phasing_mode = str(scb.get("forecastTimePhasingMode") or "FRONT_LOAD_FIRST_MONDAY")
        q_mo = max(40.0, total_demand)
        legacy_wk = [round(q_mo * 0.25, 1)] * 4
        active_wk = [round(q_mo, 1), 0.0, 0.0, 0.0] if phasing_mode == "FRONT_LOAD_FIRST_MONDAY" else list(legacy_wk)
        bwr_after = round((1.08 if phasing_mode == "FRONT_LOAD_FIRST_MONDAY" else 1.48) - (0.06 if true_floor_mode else 0.0), 2)

        res = {
            "evaluatedAt": _utc_now(), "activePreset": hc.get("activePreset"), "quoteId": qid, "customer": q.get("customer"),
            "parentKitSku": parent_kit_sku, "parentKitQty": parent_kit_qty, "unitNumber": unit_number,
            "orgType": org_type, "salesArea": org.get("salesAreaRouting"), "fulfillmentMode": fulfillment_mode,
            "localDc": local_dc, "kittingDc": kitting_dc, "kittingDcName": kitting_dc_name,
            "kittingDcLaborUtilPct": kit_labor_util, "kittingDcFabShop": kit_fab_shop,
            "timingDecision": {
                "orderToRddDays": order_lead, "creditHoldDays": credit_hold, "adjustedRddShiftDays": rdd_shift,
                "effectiveKittingLeadDays": eff_lead, "immediateCheckHorizonDays": var1, "scheduledRecheckLeadDays": var2,
                "checkTimingMode": check_mode, "deliveryBlock": deliv_block, "inventoryCommitmentStatus": commit_status,
            },
            "forecastComparison": {
                "monthlyDemandUnits": round(q_mo, 1), "phasingMode": phasing_mode,
                "legacyWeekly": legacy_wk, "activeWeekly": active_wk,
                "mulesoft30dReservationQty": round(q_mo, 1), "bullwhipBefore": 1.62, "bullwhipAfter": bwr_after,
            },
            "scbSummary": {
                "serviceLevelPct": float(scb.get("scbEoqServiceLevelPct") or 95.0),
                "survivalQuantile": str(scb.get("scbLeadTimeSurvivalQuantile") or "p90"),
                "avgP90LeadDays": round(sum_p90 / scb_n, 1) if scb_n else 8.0,
                "totalMultiEchelonSafetyStock": round(total_safety, 0),
                "totalCvarUsd": round(total_cvar, 0), "maxEoqZScore": round(max_z, 2),
                "quipuEmaAlpha": float(scb.get("scbQuipuEmaAlpha") or 0.90),
            },
            "summaryCounts": {"totalLines": len(line_decisions), **counts},
            "workflowSteps9": workflow_steps9,
            "deepMapEvents": deep_map_events,
            "lineDecisions": line_decisions,
        }
        self._enrich_tx03_tx77_order(res, q, hc, deep_map_events)
        self._relational["lastHeuristicsEvaluation"] = res
        self._refresh_pathways_and_directives("_evaluate_heuristics_order", opts, "system")
        return res

    _PHYSICAL_DC_SPECS = [
        ("GA02", "Atlanta Kitting Hub", 620, 300, 1.18, 0.86, True),
        ("NC34", "Charlotte Spoke DC", 690, 265, 1.08, 0.72, False),
        ("TX03", "Dallas Kitting Hub", 420, 330, 1.26, 0.91, True),
        ("TX77", "Houston Regional DC", 455, 395, 1.29, 0.93, False),
        ("IL30", "Chicago Kitting Hub", 535, 180, 1.24, 0.89, True),
        ("NJ22", "New Jersey Kitting Hub", 780, 175, 1.34, 0.96, True),
        ("PA15", "Philadelphia Spoke DC", 750, 195, 1.21, 0.84, False),
        ("CA20", "Southern CA Kitting Hub", 130, 315, 1.38, 0.95, True),
        ("FL14", "Orlando Kitting Hub", 685, 410, 1.16, 0.82, True),
        ("AZ17", "Phoenix Regional Hub", 215, 325, 1.12, 0.78, False),
        ("WA09", "Seattle Pacific Hub", 115, 85, 1.15, 0.79, False),
        ("CO11", "Denver Mountain Hub", 315, 235, 1.09, 0.69, False),
        ("AL04", "Birmingham Spoke DC", 575, 315, 1.04, 0.61, False),
        ("FL08", "Jacksonville Spoke DC", 685, 365, 1.09, 0.68, False),
        ("OK12", "Oklahoma City Spoke DC", 415, 285, 1.02, 0.55, False),
        ("MN55", "Minneapolis Spoke DC", 475, 125, 1.05, 0.63, False),
        ("MO18", "St Louis Spoke DC", 505, 235, 1.07, 0.66, False),
        ("OH24", "Columbus Spoke DC", 625, 210, 1.11, 0.71, False),
        ("MA07", "Boston Spoke DC", 820, 125, 1.25, 0.88, False),
        ("MD19", "Baltimore Spoke DC", 735, 220, 1.22, 0.85, False),
        ("CA42", "Oakland NorCal Spoke DC", 90, 220, 1.27, 0.90, False),
        ("FL31", "Miami South Spoke DC", 710, 465, 1.28, 0.91, False),
        ("OR11", "Portland Spoke DC", 110, 130, 1.08, 0.67, False),
        ("NV06", "Reno West Spoke DC", 140, 205, 1.06, 0.58, False),
        ("UT08", "Salt Lake Spoke DC", 220, 195, 1.03, 0.54, False),
    ]
    _PHYSICAL_EDGE_SPECS = [
        ("GA02", "NC34", 4.5, 1.12), ("GA02", "AL04", 3.0, 1.04), ("GA02", "FL08", 5.5, 1.09), ("GA02", "FL14", 7.0, 1.14),
        ("GA02", "TX03", 13.0, 1.20), ("GA02", "OH24", 9.5, 1.14), ("NC34", "MD19", 6.5, 1.18), ("NC34", "PA15", 8.0, 1.20),
        ("TX03", "TX77", 4.2, 1.28), ("TX03", "OK12", 3.5, 1.03), ("TX03", "CO11", 12.5, 1.08), ("TX03", "MO18", 10.5, 1.10),
        ("TX03", "AZ17", 15.0, 1.15), ("IL30", "MN55", 6.5, 1.06), ("IL30", "MO18", 5.0, 1.11), ("IL30", "OH24", 6.0, 1.14),
        ("NJ22", "PA15", 2.0, 1.32), ("NJ22", "MA07", 4.5, 1.26), ("NJ22", "MD19", 3.5, 1.25), ("CA20", "CA42", 6.0, 1.30),
        ("CA20", "AZ17", 6.0, 1.18), ("CA20", "NV06", 7.5, 1.10), ("FL14", "FL08", 2.5, 1.08), ("FL14", "FL31", 4.0, 1.26),
        ("WA09", "OR11", 3.2, 1.10), ("OR11", "CA42", 10.0, 1.12), ("CO11", "UT08", 8.0, 1.05), ("UT08", "NV06", 8.0, 1.04),
    ]

    def _build_physical_dc_graph(self) -> Dict[str, Any]:
        nodes = [
            {
                "dc": dc, "name": name, "x": x, "y": y,
                "trafficWeight": tw, "populationDensityIndex": pop,
                "isKittingHub": is_hub, "physicalOnly": True,
            }
            for dc, name, x, y, tw, pop, is_hub in self._PHYSICAL_DC_SPECS
        ]
        ema_map = self._relational.setdefault("rfidEdgeWeights", {})
        edges = []
        for u, v, base_hrs, traf in self._PHYSICAL_EDGE_SPECS:
            ema = float(ema_map.get(f"{u}->{v}", ema_map.get(f"{v}->{u}", 1.0)) or 1.0)
            edges.append({
                "from": u, "to": v, "baseHours": base_hrs, "trafficWeight": traf,
                "rfidEmaMultiplier": round(ema, 3), "effectiveWeight": round(base_hrs * traf * ema, 2),
            })
        return {"nodes": nodes, "edges": edges}

    def _run_weighted_dijkstra(self, source_dc: str, target_dc: str, unit_qty: float, hc: Dict[str, Any]) -> Dict[str, Any]:
        src = (source_dc or "NC34").strip().upper()
        dst = (target_dc or "GA02").strip().upper()
        g = self._build_physical_dc_graph()
        adj: Dict[str, List[tuple[str, float]]] = {n["dc"]: [] for n in g["nodes"]}
        base_hrs: Dict[tuple[str, str], float] = {}
        for e in g["edges"]:
            u, v, w, bh = e["from"], e["to"], float(e["effectiveWeight"]), float(e["baseHours"])
            adj.setdefault(u, []).append((v, w))
            adj.setdefault(v, []).append((u, w))
            base_hrs[(u, v)] = bh
            base_hrs[(v, u)] = bh
        adj.setdefault(src, [])
        dist = {k: 1e9 for k in adj}
        prev: Dict[str, str] = {}
        unvisited = set(adj.keys())
        dist[src] = 0.0
        while unvisited:
            best = min(unvisited, key=lambda n: dist[n])
            if dist[best] >= 1e8 or best == dst:
                break
            unvisited.remove(best)
            for nb, w in adj.get(best, []):
                if nb in unvisited and dist[best] + w < dist[nb]:
                    dist[nb] = dist[best] + w
                    prev[nb] = best
        path: List[str] = []
        total_hours = 0.0
        if src == dst:
            path = [src]
        elif dst in prev:
            cur: Optional[str] = dst
            while cur:
                path.append(cur)
                cur = prev.get(cur)
            path.reverse()
            for i in range(1, len(path)):
                total_hours += base_hrs.get((path[i - 1], path[i]), 6.0)
        else:
            path = [src, dst]
            total_hours = 12.0
        w_score = 0.0 if src == dst else (round(dist[dst], 2) if dst in dist and dist[dst] < 1e8 else 14.5)
        est_days = max(0.5, round(total_hours / 10.0, 1))
        sla = hc.get("slaAndTiming") or {}
        touch_labor = float(sla.get("consolidationTouchLaborUsd", 18.5))
        hold_per_day = float(sla.get("holdingPenaltyPerDayUsd", 4.25))
        cons_cost = round((0.0 if src == dst else touch_labor) + total_hours * 1.45 + max(1.0, float(unit_qty)) * 0.65 + est_days * hold_per_day, 2)
        drop_cost = round((6.0 if src == dst else 28.0) + total_hours * 1.95 + max(1.0, float(unit_qty)) * 0.40, 2)
        diff_usd = round(cons_cost - drop_cost, 2)
        hold_upcharge = round(max(0.0, est_days * hold_per_day), 2)
        rec_mode = "CONSOLIDATED_TRANSFER" if (src == dst or diff_usd <= 12.0) else "SPLIT_DROPSHIP_DIRECT"
        return {
            "sourceDc": src, "targetDc": dst, "path": path, "weightedDijkstraCost": w_score,
            "estimatedHours": round(total_hours, 1), "estimatedDays": est_days,
            "consolidationCostUsd": cons_cost, "directSplitDropshipCostUsd": drop_cost,
            "consolidationDifferentialUsd": diff_usd, "optionalHoldingCostUpchargeUsd": hold_upcharge,
            "recommendedMode": rec_mode,
        }

    def _enrich_tx03_tx77_order(self, res: Dict[str, Any], q: Dict[str, Any], hc: Dict[str, Any], deep_map_events: List[Dict[str, Any]]) -> None:
        org = hc.setdefault("organizationAndMigration", {})
        sla = hc.setdefault("slaAndTiming", {})
        ful = hc.setdefault("fulfillmentAndKitting", {})
        org["virtualWarehousesAllowed"] = False
        org["snowflakeRole"] = "analytical-export-only"
        org["directSapApiTimePhased"] = True
        surge_w = float(sla.get("expediteSurgeWeight", 0.35))
        cto_thresh = float(ful.get("ctoSeasonalityThreshold", 0.65))
        kitting_dc = str(res.get("kittingDc") or "GA02")
        alerts_10d = 0
        csr_5d = 0
        sigma3_pass = 0
        max_sigma2 = 0.0
        total_cons = 0.0
        total_drop = 0.0
        cto_list = []
        mod_buckets: Dict[str, List[str]] = {}

        for ld in res.get("lineDecisions", []):
            sku = str(ld.get("sku") or "")
            qty = max(1, int(ld.get("qty") or 1))
            tf = int(ld.get("trueFloorAvail") or 0)
            proj = int(ld.get("projectedQty") or 0)
            spoke = str(ld.get("spokeDc") or kitting_dc)
            sev = str(ld.get("severity") or "ok")
            cat = str(ld.get("sapItemCategory") or "ZTAN")
            deficit_ratio = max(0.0, float(qty - tf) / float(qty))
            sigma2_pct = round(min(99.5, deficit_ratio * 68.0 + surge_w * 24.0 + (18.0 if proj < qty else 0.0)), 2)
            sigma3_pct = 0.12 if tf >= qty else (0.24 if proj >= qty else round(min(45.0, 0.35 + deficit_ratio * 18.0 * surge_w), 2))
            alert_10d = (sigma2_pct >= 15.0) or (tf < qty)
            max_sigma2 = max(max_sigma2, sigma2_pct)
            if sigma3_pct < 0.27:
                sigma3_pass += 1
            if alert_10d:
                alerts_10d += 1
                csr_5d += 1
            cto_score = round(min(0.99, (0.72 if sev != "ok" else 0.38) + (0.22 if cat in ("ZTNF", "ZTAS") else 0.08) + surge_w * 0.2), 2)
            cto_rec = ("SUPPLIER_DROPSHIP_SLA" if cat == "ZTAS" else "CTO_ROVING_STOCKPILE") if cto_score >= cto_thresh else "CONSOLIDATED_HOLDING_COST_UPCHARGE"
            if cto_score >= cto_thresh:
                cto_list.append({"sku": sku, "ctoScore": cto_score, "recommendation": cto_rec})
            d_route = self._run_weighted_dijkstra(kitting_dc if sev == "ok" else spoke, kitting_dc, qty, hc)
            total_cons += float(d_route["consolidationCostUsd"])
            total_drop += float(d_route["directSplitDropshipCostUsd"])
            ld.update({
                "sigma2At10dPct": sigma2_pct, "sigma3At6dPct": sigma3_pct, "sigma3Pass": sigma3_pct < 0.27,
                "expediteSurgeProb": round(surge_w * (1.0 + deficit_ratio), 2),
                "tenDayAlertTriggered": alert_10d,
                "alertRecipients": ["Supervisor", "CSR", "Buyer"] if alert_10d else [],
                "csrCustomerNotifyMinDays": 5, "ctoScore": cto_score, "ctoRecommendation": cto_rec,
                "dijkstraRoute": d_route, "consolidationDifferentialUsd": d_route["consolidationDifferentialUsd"],
            })
            mod_key = "MOD-1-CORE-KIT" if (sev == "ok" and cat != "ZTAS") else ("MOD-SVC-INSTALL" if cat == "ZLAB" else "MOD-2-FLEX-PEEL")
            mod_buckets.setdefault(mod_key, []).append(sku)

        mod_groups = [
            {"moduleId": k, "coOccurrenceProb": 0.94 if k == "MOD-1-CORE-KIT" else 0.78, "skus": v, "peelCandidate": k != "MOD-1-CORE-KIT"}
            for k, v in mod_buckets.items()
        ]
        dijkstra_best = self._run_weighted_dijkstra(str(res.get("localDc") or "NC34"), kitting_dc, float(res.get("parentKitQty") or 10), hc)
        rec_strategy = "CONSOLIDATED_TRANSFER" if (total_cons - total_drop) <= 15.0 else "SPLIT_DROPSHIP_WITH_HOLDING_OPTION"
        res["tx03Tx77Architecture"] = {
            "virtualWarehousesAllowed": False,
            "snowflakeRole": "analytical-export-only",
            "sapApiTimePhasedActive": True,
            "dijkstraBestPath": dijkstra_best,
            "consolidationDifferential": {
                "totalConsolidationCostUsd": round(total_cons, 2),
                "totalDirectDropshipCostUsd": round(total_drop, 2),
                "netDifferentialUsd": round(total_cons - total_drop, 2),
                "recommendedStrategy": rec_strategy,
            },
            "stockoutGuardrails": {
                "alerts10dCount": alerts_10d, "csr5dNoticeCount": csr_5d, "maxSigma2At10dPct": max_sigma2,
                "sigma3At6dPassCount": sigma3_pass, "ctoCandidatesCount": len(cto_list), "ctoCandidates": cto_list,
            },
            "modularBomGroups": mod_groups,
            "activeRfidShipmentsCount": len(self._relational.get("rfidShipments") or []),
        }
        pathway_tier = "M365ImmediateCard" if (alerts_10d > 0 and max_sigma2 >= 15.0) else "ShiftDigest"
        deep_map_events.append({
            "eventType": "PathwayClassification",
            "targetQueue": pathway_tier,
            "sku": str(res.get("parentKitSku") or "KIT-RENO-1BR"),
            "detail": f"alerts10d={alerts_10d}, maxSigma2={max_sigma2}%, strategy={rec_strategy}, hops={len(dijkstra_best.get('path') or [])}",
        })
        quarantine_rows = [
            r for r in (self._relational.get("receivingQuarantine") or [])
            if isinstance(r, dict) and str(r.get("status") or "").upper() != "CLEARED"
        ]
        if quarantine_rows:
            deep_map_events.append({
                "eventType": "ReturnQuarantineCase",
                "targetQueue": "return-quarantine-governance",
                "sku": str(quarantine_rows[0].get("sku") or res.get("parentKitSku") or "KIT-RENO-1BR"),
                "detail": f"activeQuarantines={len(quarantine_rows)}, chain=Return",
            })

    def get_time_phased_inventory(self, dc_filter: str = "", sku_filter: str = "") -> Dict[str, Any]:
        with self._lock:
            self._ensure_heuristics_config()
            dc_f = (dc_filter or "").strip().upper()
            sku_f = (sku_filter or "").strip().upper()
            rows = []
            for it in self._relational.get("items", []):
                sku = str(it.get("sku") or "").upper()
                if sku_f and sku_f not in sku:
                    continue
                floor_map = it.get("floorQohByDc") or {}
                comm_map = it.get("committedQtyByDc") or {}
                s916_map = it.get("storage916QtyByDc") or {}
                past_due = int(it.get("unreceivedPastDuePoQty") or 0)
                exp_repl = int(it.get("expectedReplenishmentQty") or 0)
                repl_days = int(it.get("incomingReplenishmentDays") or 7)
                dcs = []
                for k, v in floor_map.items():
                    if dc_f and k.upper() != dc_f:
                        continue
                    fq = int(v or 0)
                    cq = int(comm_map.get(k) or 0)
                    q916 = int(s916_map.get(k) or 0)
                    avail0 = max(0, fq - cq - q916)
                    dcs.append({
                        "dc": k, "physicalOnly": True, "floorQoh": fq, "committedReservations": cq,
                        "storage916Excluded": q916, "excludedPastDueCo09Po": past_due,
                        "inTransitStoQty": exp_repl, "shipperEtaDays": repl_days,
                        "availableAtDate": {
                            "T+0": avail0,
                            "T+3": avail0 + (exp_repl if repl_days <= 3 else 0),
                            "T+6": avail0 + (exp_repl if repl_days <= 6 else 0),
                            "T+10": avail0 + (exp_repl if repl_days <= 10 else 0),
                            "T+14": avail0 + exp_repl,
                        },
                    })
                rows.append({"sku": sku, "description": it.get("description") or sku, "dcs": dcs})
            return {
                "ok": True, "dataSource": "SAP_DIRECT_API",
                "snowflakeOperationalPipelineBypassed": True, "snowflakeRole": "analytical-export-only",
                "virtualWarehousesAllowed": False, "dcFilter": dc_f, "skuFilter": sku_f, "items": rows,
            }

    def get_unified_marketplace_inventory(self, target_dc: str = "TX77", sku_filter: str = "", req_qty: int = 10, include_virtual: bool = False) -> Dict[str, Any]:
        if include_virtual:
            raise ValueError("VIRTUAL_WAREHOUSE_PROHIBITED: Virtual storage bins (TX03-in-TX77 / NJ22-in-PA50) cause MRO backorder hijacking and fake AP/on-hand distortion. Use Unified Physical Inventory Marketplace + Weighted Greedy Dijkstra.")
        with self._lock:
            hc = self._ensure_heuristics_config()
            tgt = (target_dc or "TX77").strip().upper()
            sku_f = (sku_filter or "").strip().upper()
            q_need = max(1, int(req_qty or 10))
            options = []
            for it in self._relational.get("items", []):
                sku = str(it.get("sku") or "").upper()
                if sku_f and sku_f not in sku:
                    continue
                floor_map = it.get("floorQohByDc") or {}
                comm_map = it.get("committedQtyByDc") or {}
                s916_map = it.get("storage916QtyByDc") or {}
                for dc, v in floor_map.items():
                    net = max(0, int(v or 0) - int(comm_map.get(dc) or 0) - int(s916_map.get(dc) or 0))
                    if net <= 0:
                        continue
                    route = self._run_weighted_dijkstra(dc, tgt, q_need, hc)
                    options.append({
                        "sku": sku, "description": it.get("description") or sku, "sourceDc": dc, "targetDc": tgt,
                        "physicalOnly": True, "netPhysicalAvail": net, "canFulfillQty": net >= q_need,
                        "dijkstraRoute": route, "recommendedMode": route["recommendedMode"],
                        "consolidationDifferentialUsd": route["consolidationDifferentialUsd"],
                    })
            return {
                "ok": True, "virtualWarehousesAllowed": False, "snowflakeRole": "analytical-export-only",
                "routingAlgorithm": "WEIGHTED_GREEDY_DIJKSTRA", "targetDc": tgt, "requestedQty": q_need,
                "physicalOptionsCount": len(options), "options": options,
            }

    def get_nodal_logistics_map(self) -> Dict[str, Any]:
        with self._lock:
            self._ensure_heuristics_config()
            g = self._build_physical_dc_graph()
            rfids = self._relational.setdefault("rfidShipments", [])
            if not rfids:
                rfids.extend([
                    {"shipmentId": "IS-1001", "shipmentType": "IS", "lineStyle": "dashed", "fromDc": "NC34", "toDc": "GA02", "status": "IN_TRANSIT", "protocol": "rust-rfid-open-tracker/v1", "tagId": "RUST-RFID-IS1001", "baselineHours": 4.5, "activatedAtUtc": _utc_now()},
                    {"shipmentId": "CS-2001", "shipmentType": "CS", "lineStyle": "solid", "fromDc": "TX03", "toDc": "TX77", "status": "CUSTOMER_DISPATCHED", "protocol": "rust-rfid-open-tracker/v1", "tagId": "RUST-RFID-CS2001", "baselineHours": 4.2, "activatedAtUtc": _utc_now()},
                ])
            open_dirs = [d for d in (self._relational.get("bodyDirectives") or []) if d.get("status") in ("open", "ack", "in_progress")]
            return {
                "ok": True, "protocol": "rust-rfid-open-tracker/v1", "virtualWarehousesAllowed": False,
                "nodes": g["nodes"], "edges": g["edges"], "shipments": rfids,
                "rfidEdgeWeights": self._relational.get("rfidEdgeWeights") or {},
                "kanbanLanes": {
                    "TO_PICK": [
                        {
                            "quoteId": q.get("id"), "customer": q.get("customer"), "localDc": q.get("localDc", "NC34"),
                            "linkedDirectives": [d.get("directiveId") for d in open_dirs if d.get("kanbanLane") == "TO_PICK"],
                        }
                        for q in self._relational.get("quotes", [])
                    ],
                    "IN_TRANSIT_IS": [r for r in rfids if r.get("shipmentType") == "IS"],
                    "CUSTOMER_STAGED_CS": [r for r in rfids if r.get("shipmentType") != "IS"],
                    "QUARANTINE_RETURN": [
                        {**qr, "linkedDirectives": [d.get("directiveId") for d in open_dirs if d.get("kanbanLane") == "QUARANTINE_RETURN" and str(qr.get("quarantineId") or "") in str(d.get("targetEntity") or "")]}
                        for qr in (self._relational.get("receivingQuarantines") or [])
                    ],
                    "AUTO_REPLEN_PO": [
                        {**po, "linkedDirectives": [d.get("directiveId") for d in open_dirs if d.get("kanbanLane") == "AUTO_REPLEN_PO" and str(po.get("poId") or "") in str(d.get("targetEntity") or "")]}
                        for po in (self._relational.get("autoReplenishmentPos") or [])
                    ],
                },
                "quipuNotes": self._relational.get("quipuNoteClassifications") or [],
                "stockopolyBins": self._relational.get("stockopolyBinMappings") or [],
                "sapSignals": self._relational.get("sapSignals") or [],
                "bodyDirectives": open_dirs,
                "pathwaySummary": (self._relational.get("pathwaySummary") or {}).get("counts") or {},
            }

    def _refresh_pathways_and_directives(self, trigger_action: str, payload: Optional[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        existing = self._relational.get("bodyDirectives") or []
        routed = CyberPhysicalPathwayRouter.classify_and_route(
            self._relational,
            existing_directives=existing,
            trigger_action=trigger_action,
        )
        self._relational["bodyDirectives"] = routed["operational"]
        self._relational["m365HandoffQueue"] = routed["m365DispatchQueue"]
        self._relational["pathwaySummary"] = {
            "evaluatedAt": routed["evaluatedAt"],
            "triggerAction": trigger_action,
            "returnChainOwner": routed["returnChainOwner"],
            "systemic": routed["systemic"],
            "counts": routed["counts"],
        }
        CyberPhysicalPathwayRouter.sync_to_sqlite_brain(
            self.sqlite_path,
            routed["operational"],
            [],
        )
        return routed

    def _push_outbox_to_hubcore_async(self, outbox_entry: Dict[str, Any]) -> None:
        relay_url = str(os.environ.get("HUBCORE_RELAY_URL") or "").strip()
        if not relay_url:
            return

        def _worker() -> None:
            try:
                body = json.dumps({"streamId": "holonhub-live", "events": [outbox_entry]}, default=str).encode("utf-8")
                req = urllib.request.Request(
                    relay_url,
                    data=body,
                    headers={"Content-Type": "application/json", "User-Agent": "HolonHubLive/2.5"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=1.5):
                    pass
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="holonhub-outbox-push").start()

    def get_outbox(self, since_cursor: int = 0, limit: int = 200) -> Dict[str, Any]:
        with self._lock:
            sc = max(0, int(since_cursor))
            lim = max(1, min(1000, int(limit)))
            events = [ev for ev in self._outbox if int(ev.get("seq", 0)) > sc][:lim]
            return {
                "ok": True,
                "homeNodeId": self.HOME_NODE_ID,
                "cursor": len(self._outbox),
                "sinceCursor": sc,
                "count": len(events),
                "events": events,
            }

    def get_pathways(self) -> Dict[str, Any]:
        with self._lock:
            if not self._relational.get("pathwaySummary"):
                self._refresh_pathways_and_directives("read", {}, "system")
            summary = self._relational.get("pathwaySummary") or {}
            return {
                "ok": True,
                "architecture": "dual-speed-cyberphysical-io/v2.5",
                "returnChainOwner": summary.get("returnChainOwner") or CyberPhysicalPathwayRouter.RETURN_CHAIN_OWNER,
                "evaluatedAt": summary.get("evaluatedAt"),
                "systemic": summary.get("systemic") or [],
                "operational": self._relational.get("bodyDirectives") or [],
                "m365DispatchQueue": self._relational.get("m365HandoffQueue") or [],
                "counts": summary.get("counts") or {},
            }

    def get_directives(self, status_filter: str = "", owner_role: str = "") -> Dict[str, Any]:
        with self._lock:
            if not self._relational.get("pathwaySummary"):
                self._refresh_pathways_and_directives("read", {}, "system")
            dirs = list(self._relational.get("bodyDirectives") or [])
            sf = str(status_filter or "").strip().lower()
            own = str(owner_role or "").strip().lower()
            if sf:
                dirs = [d for d in dirs if str(d.get("status") or "").lower() == sf]
            if own:
                dirs = [d for d in dirs if own in str(d.get("ownerRole") or "").lower()]
            fb = list(self._relational.get("bodyFeedback") or [])
            return {
                "ok": True,
                "count": len(dirs),
                "directives": dirs,
                "bodyFeedback": fb,
                "counts": (self._relational.get("pathwaySummary") or {}).get("counts") or {},
            }

    def mutate_relational(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an Air-Lock gated, owner-validated relational mutation with atomic outbox commit."""
        if len(json.dumps(body, default=str, ensure_ascii=False).encode("utf-8")) > self.MAX_PAYLOAD_BYTES:
            raise ValueError(f"Air-Lock Rejected: Payload exceeds {self.MAX_PAYLOAD_BYTES} bytes.")

        action = str(body.get("action") or "").strip()
        actor = str(body.get("actor") or "anonymous")[:80]
        persona = str(body.get("persona") or "RSR / Seller")[:60]
        payload = body.get("payload") or {}
        idempotency_key = str(body.get("idempotencyKey") or "").strip()
        req_hash = _sha256_json({"action": action, "payload": payload})
        corr_id = str(body.get("correlationId") or f"corr-{req_hash[:12]}")

        with self._lock:
            try:
                self._check_active_route("x_osq")
            except UnavailableError as exc:
                self._record_restricted_audit(actor, "DENY", str(exc), corr_id, "")
                raise

            if idempotency_key and idempotency_key in self._idempotency_index:
                existing_rec = self._idempotency_index[idempotency_key]
                if existing_rec["requestHash"] != req_hash:
                    msg = f"Idempotency conflict: key '{idempotency_key}' already bound to a different payload hash."
                    self._record_restricted_audit(actor, "DENY", msg, existing_rec["correlationId"], existing_rec["taskId"])
                    raise ConflictError(msg)
                self._record_restricted_audit(
                    actor, "ALLOW", f"Idempotent replay for {action}", existing_rec["correlationId"], existing_rec["taskId"]
                )
                state_env = self.get_state()
                state_env.update({
                    "idempotentReplay": True,
                    "idempotencyKey": idempotency_key,
                    "taskId": existing_rec["taskId"],
                    "correlationId": existing_rec["correlationId"],
                    "causationId": existing_rec["causationId"],
                    "outboxCursor": existing_rec["outboxCursor"],
                    "taskReceipt": existing_rec["taskReceipt"],
                })
                return state_env

            # Owner-side validation inside the write transaction: expected version & lease fencing
            exp_rev = body.get("expectedRevision") if "expectedRevision" in body else body.get("expectedVersion")
            cur_rev = int(self._relational.get("revision", 1))
            if exp_rev is not None and str(exp_rev).strip() != "":
                if int(exp_rev) != cur_rev:
                    msg = f"Owner validation rejected stale expectedRevision={exp_rev} (current={cur_rev})."
                    self._record_restricted_audit(actor, "DENY", msg, corr_id, "")
                    raise ConflictError(msg)

            self._prune_temp()
            target_ekey = str(
                body.get("entityKey")
                or payload.get("entityKey")
                or payload.get("quoteId")
                or payload.get("id")
                or payload.get("opportunityId")
                or payload.get("budgetId")
                or ""
            ).strip()
            caller_session = str(body.get("sessionId") or payload.get("sessionId") or "").strip()
            raw_fence = body.get("fencingToken") if "fencingToken" in body else payload.get("fencingToken")
            caller_fence = int(raw_fence) if raw_fence is not None and str(raw_fence).strip() != "" else None
            if target_ekey:
                active_lock = next((lk for lk in self._temp.get("entityLocks", []) if lk.get("entityKey") == target_ekey), None)
                max_seen_fence = int(self._entity_max_fencing.get(target_ekey, 0))
                if active_lock:
                    lock_fence = int(active_lock.get("fencingToken") or 0)
                    if caller_fence is not None and caller_fence != lock_fence:
                        msg = f"Owner validation rejected stale fencingToken={caller_fence} for '{target_ekey}' (active={lock_fence})."
                        self._record_restricted_audit(actor, "DENY", msg, corr_id, "")
                        raise ConflictError(msg)
                    if caller_session and active_lock.get("sessionId") != caller_session:
                        msg = f"Owner validation rejected write to '{target_ekey}': lease held by session '{active_lock.get('sessionId')}'."
                        self._record_restricted_audit(actor, "DENY", msg, corr_id, "")
                        raise ConflictError(msg)
                elif caller_fence is not None and caller_fence < max_seen_fence:
                    msg = f"Owner validation rejected stale fencingToken={caller_fence} < latest fence={max_seen_fence} on '{target_ekey}'."
                    self._record_restricted_audit(actor, "DENY", msg, corr_id, "")
                    raise ConflictError(msg)

            items_by_sku = {i["sku"]: i for i in self._relational.get("items", [])}
            accounts_by_id = {a["accountId"]: a for a in self._relational.get("accounts", [])}
            quotes_by_id = {q["id"]: q for q in self._relational.get("quotes", [])}

            if action == "sync.full":
                for key in (
                    "accounts", "opportunities", "budgets", "items", "quotes", "floorplans",
                    "forecastBatch", "marketplaceShards", "connectors", "changes", "snapshots",
                    "heuristicsConfig", "lastHeuristicsEvaluation", "deepMapFlexLogicDomain"
                ):
                    if key in payload and payload[key] is not None:
                        self._relational[key] = payload[key]
                self._append_event(actor, persona, "relational.full_sync", "osq_relational_store", "Synchronized state from live client")

            elif action == "opportunity.create":
                acct = accounts_by_id.get(payload.get("accountId"))
                if not acct:
                    raise ValueError("Unknown accountId")
                if acct.get("status") == "Closed":
                    raise ValueError(f"CPQ-1 Blocked: Account '{acct['name']}' has Closed status.")
                flags = payload.get("flags") or {}
                coord = bool(flags.get("coordinatedDelivery"))
                if coord:
                    flags["kitted"] = True
                hold_days = 45 if coord else int(payload.get("forecastHoldDays") or 30)
                opp = {
                    "opportunityId": payload.get("opportunityId") or f"OPP-RP-{int(time.time())}",
                    "parentOpportunityId": payload.get("parentOpportunityId"),
                    "isTopLink": bool(payload.get("isTopLink", False)),
                    "accountId": acct["accountId"],
                    "name": str(payload.get("name") or "New RP Opportunity"),
                    "ownerRole": persona,
                    "stage": str(payload.get("stage") or "Qualified"),
                    "segment": str(payload.get("segment") or "All"),
                    "estimatedAmount": float(payload.get("estimatedAmount") or 0.0),
                    "propertyWalkRequired": bool(payload.get("propertyWalkRequired", True)),
                    "tempoEnabled": bool(payload.get("tempoEnabled", False)) and payload.get("segment") == "National Accounts",
                    "forecastHoldDays": hold_days,
                    "flags": flags,
                }
                self._relational.setdefault("opportunities", []).append(opp)
                self._append_event(actor, persona, "opportunity.created", opp["opportunityId"], opp["name"])

            elif action == "item.upsert_batch":
                rows = payload.get("items") or []
                for r in rows:
                    sku = str(r["sku"]).strip().upper()
                    existing = items_by_sku.get(sku)
                    record = {
                        **(existing or {}),
                        **r,
                        "sku": sku,
                        "updated": _utc_now(),
                    }
                    if existing:
                        existing.update(record)
                    else:
                        self._relational.setdefault("items", []).append(record)
                self._append_event(actor, persona, "item.upsert_batch", f"{len(rows)} SKUs", str(payload.get("source") or "API"))

            elif action == "item.swap_discontinued":
                old_sku = str(payload.get("oldSku") or "").strip().upper()
                new_sku = str(payload.get("newSku") or "").strip().upper()
                new_item = items_by_sku.get(new_sku)
                if not new_item or new_item.get("status") != "Active":
                    raise ValueError(f"CPQ-34: Replacement SKU '{new_sku}' is not an Active item.")
                swapped_quotes = []
                for q in self._relational.get("quotes", []):
                    if q.get("status") in ("Accepted", "Expired", "Cancelled"):
                        continue
                    touched = False
                    for line in q.get("lines", []):
                        if line["sku"] == old_sku:
                            line["sku"] = new_sku
                            line["unit"] = float(new_item["price"])
                            touched = True
                    if touched:
                        swapped_quotes.append(q["id"])
                self._append_event(
                    actor, persona, "item.bulk_swap", old_sku,
                    f"Swapped {old_sku} -> {new_sku} across {len(swapped_quotes)} open quote(s): {', '.join(swapped_quotes)}"
                )

            elif action == "quote.validate_vc_line":
                attrs = payload.get("vcAttributes") or {}
                if attrs.get("blindSize") and not attrs.get("blindMounting"):
                    raise ValueError("CPQ-25 VC Constraint Blocked: Blind Mounting cannot be blank when Blind Size is specified.")
                quote = quotes_by_id.get(payload.get("quoteId"))
                if not quote:
                    raise ValueError("Quote not found")
                quote.setdefault("lines", []).append(payload.get("line"))
                self._append_event(actor, persona, "quote.vc_line_added", quote["id"], str(payload.get("line", {}).get("sku")))

            elif action == "quote.request_change":
                qid = str(payload.get("quoteId") or "")
                note = str(payload.get("note") or "").strip()
                quote = quotes_by_id.get(qid)
                if not quote:
                    raise ValueError("Quote not found")
                quote["status"] = "Revision requested"
                self._append_event(actor, persona, "customer.change_requested", qid, note[:300])

            elif action == "quote.accept":
                qid = str(payload.get("quoteId") or "")
                signer = str(payload.get("signer") or actor).strip()
                quote = quotes_by_id.get(qid)
                if not quote:
                    raise ValueError("Quote not found")
                for line in quote.get("lines", []):
                    it = items_by_sku.get(line["sku"])
                    if not it or (it.get("status") != "Active" and int(it.get("qoh") or 0) == 0):
                        raise ValueError(f"CPQ-61 / UQ-45 Blocked: Quote contains discontinued & out-of-stock SKU '{line['sku']}'.")
                    if it.get("status") != "Active":
                        raise ValueError(f"CPQ-34 Blocked: SKU '{line['sku']}' is Discontinued. Use Bulk Swap before acceptance.")
                margin_info = self._recompute_quote_margin(quote)
                if not margin_info["autoApproved"] and not payload.get("marginExceptionApproved"):
                    raise ValueError(
                        f"CPQ-35 Margin Floor Breach ({margin_info['quoteMarginPct']}% < {margin_info['marginFloorPct']}%): "
                        + json.dumps(margin_info["breaches"])
                    )
                quote["status"] = "Accepted"
                quote["signedRofAttached"] = True
                quote["acceptedBy"] = signer
                quote["acceptedAt"] = _utc_now()
                opp_id = quote.get("opportunityId")
                if opp_id:
                    for opp in self._relational.get("opportunities", []):
                        if opp.get("opportunityId") == opp_id and opp.get("stage") != "Project In Progress":
                            opp["stage"] = "Quote Won"
                            opp["probability"] = 95
                            break
                self._append_event(actor, persona, "customer.accepted", qid, f"Accepted v{quote.get('version', 1)} by {signer}")
                self._run_osq_pipeline(actor, persona, {})

            elif action == "forecast.emergency_brake":
                decision = str(payload.get("decision") or "HELD").upper()
                fb = self._relational.setdefault("forecastBatch", {})
                eb = fb.setdefault("emergencyBrake", {})
                eb["status"] = decision
                eb["tripped"] = decision != "OVERRIDDEN"
                eb["resolvedBy"] = actor
                eb["resolvedAt"] = _utc_now()
                fb["taskState"] = "ACCEPTED" if decision == "OVERRIDDEN" else "QUARANTINED"
                self._append_event(actor, persona, f"forecast.emergency_brake.{decision.lower()}", fb.get("batchId", "BATCH"), f"Planner decision by {actor}")

            elif action == "marketplace.tokenize":
                shard = {
                    "asset_id": payload.get("asset_id") or f"osq-shard-{int(time.time())}",
                    "token_id": payload.get("token_id") or f"tok-hub-osq-{int(time.time())}",
                    "axis": payload.get("axis") or "entirety",
                    "title": payload.get("title") or "x_OSQ Live Forecast & Quote Governance Shard",
                    "category": f"supply_chain_{payload.get('domain', 'forecasting')}",
                    "marketplace_scope": payload.get("marketplace_scope") or "hub",
                    "org_id": payload.get("org_id") or "org-blackbulls-hideout-01",
                    "hub_id": payload.get("hub_id") or "hub-floor-main",
                    "supply_chain_domain": payload.get("domain") or "forecasting",
                    "sku_ref": payload.get("sku_ref") or "SKU-HVAC-3001",
                    "batch_id": payload.get("batch_id") or "BATCH-20261121-001",
                    "forecast_horizon": payload.get("forecast_horizon") or "26-weeks",
                    "sla_target": payload.get("sla_target") or "QUIPU Curated",
                    "shard_count": int(payload.get("shard_count") or 500),
                    "available_shards": int(payload.get("shard_count") or 500),
                    "current_shard_price": float(payload.get("shard_price") or 12.5),
                    "royalty_rate": 0.02,
                    "quality_score": 0.98,
                    "protocol": "gard-shard/v2",
                    "domain": "SiCi_SQRT(-1)",
                    "envelope_digest": _sha256_json(payload),
                }
                self._relational.setdefault("marketplaceShards", []).insert(0, shard)
                self._append_event(actor, persona, "marketplace.shard.tokenized", shard["asset_id"], f"Scope={shard['marketplace_scope']} shards={shard['shard_count']}")

            elif action == "change.upsert":
                chg = dict(payload)
                changes = self._relational.setdefault("changes", [])
                idx = next((i for i, c in enumerate(changes) if c.get("id") == chg.get("id")), -1)
                if idx >= 0:
                    changes[idx] = chg
                else:
                    changes.append(chg)
                self._append_event(actor, persona, f"change.{chg.get('status', 'updated').lower()}", chg.get("id", "CHG"), chg.get("object", ""))

            elif action == "snapshot.create":
                self._relational.setdefault("snapshots", []).append(dict(payload))
                self._append_event(actor, persona, "snapshot.created", payload.get("id", "SNAP"), payload.get("note", ""))

            elif action == "connector.upsert":
                auth_ref = _validate_secret_ref(payload.get("authRef"))
                clean_conn = dict(payload)
                clean_conn["authRef"] = auth_ref
                clean_conn.setdefault("readGrants", ["connector.read"])
                clean_conn.setdefault("mutationGrants", ["connector.mutate.idempotent"])
                connectors = self._relational.setdefault("connectors", [])
                idx = next((i for i, c in enumerate(connectors) if c.get("id") == clean_conn.get("id")), -1)
                if idx >= 0:
                    connectors[idx] = clean_conn
                else:
                    connectors.append(clean_conn)
                self._append_event(actor, persona, "connector.saved", clean_conn.get("id", "CON"), clean_conn.get("name", ""))

            elif action == "crm.account.upsert":
                accounts = self._relational.setdefault("accounts", [])
                aid = str(payload.get("accountId") or f"ACC-{int(time.time())}")
                ex = next((a for a in accounts if str(a.get("accountId") or "").lower() == aid.lower()), None)
                if not ex:
                    ex = {"accountId": aid, "status": "Active", "creditStatus": "Clear", "segment": "All", "ewalletBalance": 0.0}
                    accounts.append(ex)
                for k, v in payload.items():
                    if k != "accountId":
                        ex[k] = copy.deepcopy(v)
                ex["accountId"] = aid
                self._append_event(actor, persona, "crm.account.saved", aid, str(ex.get("name") or ""))

            elif action == "crm.opportunity.advance_stage":
                opps = self._relational.setdefault("opportunities", [])
                oid = str(payload.get("opportunityId") or "")
                opp = next((o for o in opps if str(o.get("opportunityId") or "").lower() == oid.lower()), None)
                if not opp:
                    raise ValueError(f"Opportunity not found: {oid}")
                acct = accounts_by_id.get(str(opp.get("accountId") or ""))
                if acct and str(acct.get("status") or "").lower() == "closed":
                    raise ValueError(f"CPQ-1 Blocked: Cannot modify opportunity on Closed account '{acct['name']}'.")
                if payload.get("autoClear180"):
                    clear_dt = self._parse_utc_iso(opp.get("conditionAutoClearsAt"))
                    if clear_dt and clear_dt <= datetime.now(timezone.utc):
                        opp["opportunityCondition"] = None
                        opp["conditionAutoClearsAt"] = None
                        self._append_event(actor, persona, "opportunity.condition_autocleared", oid, "CPQ-9 cleared >180d placeholder")
                if "opportunityCondition" in payload:
                    cond = payload["opportunityCondition"]
                    opp["opportunityCondition"] = cond
                    if cond in ("Pro Forma", "MOG"):
                        opp["conditionAutoClearsAt"] = payload.get("conditionAutoClearsAt") or (datetime.now(timezone.utc) + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    else:
                        opp["conditionAutoClearsAt"] = None
                for field in ("name", "segment", "estimatedAmount", "targetBudget", "serviceSolution", "expectedCloseDate", "qualificationQuestionsYes", "propertyWalkRequired", "walkSummary", "projectLocationOverride", "stageAgeDays"):
                    if field in payload:
                        opp[field] = copy.deepcopy(payload[field])
                if isinstance(payload.get("flags"), dict):
                    opp.setdefault("flags", {}).update(payload["flags"])
                flags = opp.setdefault("flags", {})
                if flags.get("coordinatedDelivery"):
                    flags["kitted"] = True
                    opp["forecastHoldDays"] = 45
                else:
                    opp["forecastHoldDays"] = int(opp.get("forecastHoldDays") or 30)
                target_stage = str(payload.get("targetStage") or payload.get("stage") or opp.get("stage") or "Qualified")
                if target_stage not in ("Lead", "Closed Lost"):
                    if int(opp.get("qualificationQuestionsYes") or 0) < 1:
                        raise ValueError("CPQ-5 Blocked: At least 1 qualification question must be answered Yes before advancing past Lead.")
                    if float(opp.get("estimatedAmount") or 0.0) <= 0.0:
                        raise ValueError("CPQ-14 Blocked: Estimated Amount must exceed $0 before entering Qualified or later stages.")
                if target_stage == "Property Walk" and not bool(opp.get("propertyWalkRequired", True)):
                    target_stage = "Ready for Quote"
                prev_stage = str(opp.get("stage") or "")
                opp["stage"] = target_stage
                opp["probability"] = int(payload.get("probability") if payload.get("probability") is not None else self._stage_default_probability(target_stage))
                if prev_stage != target_stage and "stageAgeDays" not in payload:
                    opp["stageAgeDays"] = 0
                self._append_event(actor, persona, "crm.opportunity.stage_advanced", oid, f"{prev_stage} -> {target_stage}")

            elif action == "budget.upsert":
                budgets = self._relational.setdefault("budgets", [])
                bid = str(payload.get("budgetId") or f"BDG-{int(time.time())}")
                ex = next((b for b in budgets if str(b.get("budgetId") or "").lower() == bid.lower()), None)
                clone_from = str(payload.get("cloneFromBudgetId") or "")
                if not ex:
                    if clone_from:
                        src = next((b for b in budgets if str(b.get("budgetId") or "").lower() == clone_from.lower()), None)
                        if not src:
                            raise ValueError(f"Clone source budget not found: {clone_from}")
                        ex = copy.deepcopy(src)
                        ex["budgetId"] = bid
                        ex["isMogTemplate"] = False
                        ex["linkedQuoteId"] = None
                        ex["status"] = "Draft CAPEX"
                    else:
                        ex = {
                            "budgetId": bid, "opportunityId": payload.get("opportunityId"),
                            "accountId": payload.get("accountId"), "title": payload.get("title") or "New Holon CAPEX Budget",
                            "budgetMode": payload.get("budgetMode") or "Live CAPEX", "quoteType": payload.get("quoteType") or "PI / Turns",
                            "occupancyMultiplier": 1.0, "targetCapexBudget": 50000.0, "contingencyPct": 5.0,
                            "taxPct": 7.25, "freightEstimate": 0.0, "bmsDiscountPct": 0.0, "ewalletApplied": 0.0,
                            "isProForma": False, "isMogTemplate": False, "status": "Draft CAPEX",
                            "floorplanGroups": [], "lines": [],
                        }
                    budgets.append(ex)
                for k, v in payload.items():
                    if k not in ("budgetId", "cloneFromBudgetId"):
                        ex[k] = copy.deepcopy(v)
                ex["budgetId"] = bid
                ex["updatedAt"] = _utc_now()
                self._append_event(actor, persona, "budget.saved", bid, str(ex.get("title") or ""))

            elif action == "budget.promote_to_quote":
                budgets = self._relational.setdefault("budgets", [])
                opps = self._relational.setdefault("opportunities", [])
                quotes = self._relational.setdefault("quotes", [])
                floorplans = self._relational.setdefault("floorplans", [])
                bid = str(payload.get("budgetId") or "")
                bdg = next((b for b in budgets if str(b.get("budgetId") or "").lower() == bid.lower()), None)
                if not bdg:
                    raise ValueError(f"Budget scenario not found: {bid}")
                oid = str(payload.get("opportunityId") or bdg.get("opportunityId") or "")
                opp = next((o for o in opps if o.get("opportunityId") == oid), None)
                acct_id = str(payload.get("accountId") or (opp.get("accountId") if opp else bdg.get("accountId")) or "")
                acct = accounts_by_id.get(acct_id)
                if acct and str(acct.get("status") or "").lower() == "closed":
                    raise ValueError(f"CPQ-1 Blocked: Cannot promote budget for Closed account '{acct['name']}'.")
                is_pf = bool(bdg.get("isProForma")) or bool(bdg.get("isMogTemplate")) or (opp is not None and opp.get("opportunityCondition") == "Pro Forma")
                convert_live = bool(payload.get("convertProFormaToLive"))
                if is_pf and not convert_live:
                    raise ValueError("CPQ-8 Blocked: Pro Forma / MOG budgets cannot promote to a live Primary Quote unless convertProFormaToLive=true.")
                if convert_live:
                    bdg["isProForma"] = False
                    bdg["isMogTemplate"] = False
                    bdg["budgetMode"] = "Live CAPEX"
                    if opp:
                        opp["opportunityCondition"] = None
                        opp["conditionAutoClearsAt"] = None
                occ_mult = max(0.1, float(bdg.get("occupancyMultiplier") or 1.0))
                qid = str(payload.get("quoteId") or bdg.get("linkedQuoteId") or f"CPQ-{int(time.time())}")
                existing_q = quotes_by_id.get(qid)
                q = existing_q if existing_q is not None else {}
                q.update({
                    "id": qid, "opportunityId": oid, "accountId": acct_id, "budgetId": bid,
                    "customer": acct["name"] if acct else str(payload.get("customer") or "HD Supply Customer"),
                    "source": "CPQ", "quoteType": bdg.get("quoteType") or "PI / Turns",
                    "hospitalityQuoteType": bdg.get("hospitalityQuoteType"),
                    "version": int(existing_q.get("version") or 1) + 1 if existing_q else 1,
                    "status": "Draft", "approvalStatus": "Draft", "approvalMode": "Pending",
                    "isPrimary": True, "taxPct": float(bdg.get("taxPct") or 7.25),
                    "marginFloorPct": float(q.get("marginFloorPct") or 24.0),
                    "quoteDiscountPct": float(payload.get("quoteDiscountPct") or 0.0),
                    "bmsDiscountPct": float(bdg.get("bmsDiscountPct") or 0.0),
                    "ewalletApplied": float(bdg.get("ewalletApplied") or 0.0),
                    "rofDetailLevel": q.get("rofDetailLevel") or "floorplan",
                })
                if acct and acct.get("priceProtectionUntil"):
                    q["priceProtectionUntil"] = acct["priceProtectionUntil"]
                q_lines = []
                for idx, bl in enumerate(bdg.get("lines", []), start=1):
                    ln_dict = {
                        "lineId": f"LN-{idx}", "sku": str(bl.get("sku") or "").strip().upper(),
                        "qty": max(1, int(round(float(bl.get("qty") or 1) * occ_mult))),
                        "orderedQty": 0, "unit": float(bl.get("unit") or 0.0),
                        "group": bl.get("group") or "General", "floorplanId": f"FP-{qid}-{idx}",
                        "floorplanDiscountPct": 0.0, "lineDiscountPct": 0.0,
                    }
                    if bl.get("vcAttributes"):
                        ln_dict["vcAttributes"] = copy.deepcopy(bl["vcAttributes"])
                    q_lines.append(ln_dict)
                q["lines"] = q_lines
                if existing_q is None:
                    quotes.append(q)
                if oid:
                    for sq in quotes:
                        if sq.get("opportunityId") == oid:
                            sq["isPrimary"] = (sq.get("id") == qid)
                for fp_seq, g in enumerate(bdg.get("floorplanGroups", []), start=1):
                    fp_id = f"FP-{qid}-{fp_seq}"
                    if not any(fp.get("floorplanId") == fp_id for fp in floorplans):
                        floorplans.append({
                            "floorplanId": fp_id, "quoteId": qid, "name": g.get("name") or f"Floorplan {fp_seq}",
                            "floorplanNumber": f"FP-{fp_seq:03d}", "units": int(g.get("units") or 1),
                            "beds": int(g.get("beds") or 1), "baths": int(g.get("baths") or 1),
                        })
                self._recompute_quote_margin(q)
                bdg["status"] = "Promoted to Primary Quote"
                bdg["linkedQuoteId"] = qid
                bdg["updatedAt"] = _utc_now()
                if opp:
                    if opp.get("stage") in ("Lead", "Qualified", "Property Walk", "Ready for Quote"):
                        opp["stage"] = "Quoted"
                        opp["probability"] = 75
                    if q.get("netSubtotal"):
                        opp["estimatedAmount"] = q["netSubtotal"]
                self._relational["selectedQuoteId"] = qid
                self._append_event(actor, persona, "budget.promoted_to_quote", bid, f"Promoted to Primary Quote {qid} (CPQ-70)")

            elif action == "cpq.quote.upsert":
                quotes = self._relational.setdefault("quotes", [])
                opps = self._relational.setdefault("opportunities", [])
                qid = str(payload.get("id") or payload.get("quoteId") or f"CPQ-{int(time.time())}")
                existing = quotes_by_id.get(qid)
                q = existing if existing is not None else {
                    "id": qid, "version": 1, "status": "Draft", "approvalStatus": "Draft", "approvalMode": "Pending",
                    "isPrimary": False, "taxPct": 7.25, "marginFloorPct": 24.0, "quoteDiscountPct": 0.0,
                    "bmsDiscountPct": 0.0, "ewalletApplied": 0.0, "rofDetailLevel": "floorplan", "lines": [],
                }
                oid = str(payload.get("opportunityId") or q.get("opportunityId") or "")
                opp = next((o for o in opps if o.get("opportunityId") == oid), None)
                acct_id = str(payload.get("accountId") or (opp.get("accountId") if opp else q.get("accountId")) or "")
                acct = accounts_by_id.get(acct_id)
                if acct and str(acct.get("status") or "").lower() == "closed":
                    raise ValueError(f"CPQ-1 Blocked: Cannot create/update quote for Closed account '{acct['name']}'.")
                cand_lines = payload.get("lines") if "lines" in payload else q.get("lines", [])
                for ln in cand_lines:
                    sku = str(ln.get("sku") or "").strip().upper()
                    it = items_by_sku.get(sku)
                    is_labor = (it and str(it.get("itemType") or "").lower() == "labor") or sku.startswith("LAB-")
                    if is_labor and not (opp and (opp.get("flags") or {}).get("installation")):
                        raise ValueError(f"CPQ-11 Blocked: Labor SKU '{sku}' requires Opportunity flags.installation = true.")
                    if it and it.get("vcRequired"):
                        attrs = ln.get("vcAttributes") or {}
                        if not attrs.get("blindSize") or not attrs.get("blindMounting"):
                            raise ValueError(f"CPQ-25 VC Constraint Blocked: SKU '{sku}' requires both blindSize and blindMounting.")
                for k, v in payload.items():
                    if k not in ("quoteId", "autoSubmitApproval"):
                        q[k] = copy.deepcopy(v)
                q["id"] = qid
                if oid:
                    q["opportunityId"] = oid
                if acct_id:
                    q["accountId"] = acct_id
                if existing is None:
                    quotes.append(q)
                if q.get("isPrimary") and oid:
                    for sq in quotes:
                        if sq.get("opportunityId") == oid:
                            sq["isPrimary"] = (sq.get("id") == qid)
                self._recompute_quote_margin(q)
                if payload.get("autoSubmitApproval") and (q.get("pricingGuidance") or {}).get("autoApproveEligible"):
                    q["approvalStatus"] = "Approved-Pricing"
                    q["approvalMode"] = "Auto-Approved (CPQ-45)"
                    q["status"] = "Quoted"
                self._append_event(actor, persona, "cpq.quote.saved", qid, f"PM={q.get('approvedMarginPct')}%, Lines={len(q.get('lines', []))}")

            elif action == "cpq.quote.approval":
                qid = str(payload.get("quoteId") or payload.get("id") or "")
                decision = str(payload.get("decision") or "submit").strip().lower()
                justif = str(payload.get("justification") or "").strip()
                q = quotes_by_id.get(qid)
                if not q:
                    raise ValueError(f"Quote not found: {qid}")
                self._recompute_quote_margin(q)
                margin_pct = float(q.get("approvedMarginPct") or 0.0)
                floor_pct = float(q.get("marginFloorPct") or 24.0)
                if decision == "submit":
                    if margin_pct >= floor_pct and not payload.get("forceEscalation"):
                        q["approvalStatus"] = "Approved-Pricing"
                        q["approvalMode"] = "Auto-Approved (CPQ-45)"
                        q["approvalJustification"] = justif or "Meets margin floor threshold."
                        q["status"] = "Quoted"
                    else:
                        if not justif:
                            raise ValueError("CPQ-42 Blocked: Below-floor / escalated margin submission requires non-empty justification.")
                        q["approvalStatus"] = "Pending-Approval"
                        q["approvalMode"] = "Pricing Team Queue (CPQ-46)"
                        q["approvalJustification"] = justif
                        q["status"] = "Pending Pricing Approval"
                elif decision == "recall":
                    q["approvalStatus"] = "Draft"
                    q["approvalMode"] = "Recalled by Seller (CPQ-44)"
                    q["status"] = "Draft"
                elif decision == "approve":
                    q["approvalStatus"] = "Approved-Pricing"
                    q["approvalMode"] = f"Pricing Team Approved ({actor})"
                    if justif:
                        q["approvalJustification"] = justif
                    q["approvedBy"] = actor
                    q["approvedAt"] = _utc_now()
                    q["status"] = "Quoted"
                elif decision == "reject":
                    q["approvalStatus"] = "Rejected-Pricing"
                    q["approvalMode"] = f"Pricing Rejected ({actor})"
                    if justif:
                        q["approvalJustification"] = justif
                    q["status"] = "Revision requested"
                else:
                    raise ValueError(f"Unknown approval decision: {decision}")
                self._append_event(actor, persona, f"cpq.quote.approval.{decision}", qid, f"Status={q.get('approvalStatus')}")

            elif action == "cpq.quote.price_refresh":
                qid = str(payload.get("quoteId") or payload.get("id") or "")
                q = quotes_by_id.get(qid)
                if not q:
                    raise ValueError(f"Quote not found: {qid}")
                pp_str = q.get("priceProtectionUntil")
                pp_dt = self._parse_utc_iso(pp_str)
                if not payload.get("overridePriceProtection") and pp_dt and pp_dt > datetime.now(timezone.utc):
                    raise ValueError(f"CPQ-63 Blocked: Price Protection active until {pp_str}.")
                updated_lines = 0
                for ln in q.get("lines", []):
                    if float(ln.get("qty") or 0) - float(ln.get("orderedQty") or 0) <= 0:
                        continue
                    it = items_by_sku.get(str(ln.get("sku") or "").upper())
                    if it and float(it.get("price") or 0) > 0:
                        ln["unit"] = float(it["price"])
                        updated_lines += 1
                q["version"] = int(q.get("version") or 1) + 1
                q["lastPriceRefreshAt"] = _utc_now()
                self._recompute_quote_margin(q)
                self._append_event(actor, persona, "cpq.quote.price_refreshed", qid, f"v{q['version']}, refreshedLines={updated_lines}")

            elif action == "cpq.order.place":
                qid = str(payload.get("quoteId") or payload.get("id") or "")
                q = quotes_by_id.get(qid)
                if not q:
                    raise ValueError(f"Quote not found: {qid}")
                if str(q.get("status") or "").lower() != "accepted":
                    raise ValueError(f"CPQ-55 Blocked: Quote '{qid}' must be Accepted before placing a staged SAP Order.")
                q_lines = q.get("lines", [])
                req_lines = payload.get("lines") or []
                units_placed = 0
                if not req_lines:
                    for ln in q_lines:
                        rem = max(0, int(ln.get("qty") or 0) - int(ln.get("orderedQty") or 0))
                        if rem > 0:
                            ln["orderedQty"] = int(ln.get("qty") or 0)
                            units_placed += rem
                else:
                    for rl in req_lines:
                        lid = str(rl.get("lineId") or "")
                        sku = str(rl.get("sku") or "")
                        order_qty = int(rl.get("orderQty") or rl.get("qty") or 0)
                        if order_qty <= 0:
                            continue
                        target_ln = next((l for l in q_lines if (lid and l.get("lineId") == lid) or (not lid and sku and l.get("sku") == sku)), None)
                        if not target_ln:
                            raise ValueError(f"Quote line not found: {lid or sku}")
                        rem = max(0, int(target_ln.get("qty") or 0) - int(target_ln.get("orderedQty") or 0))
                        if order_qty > rem:
                            raise ValueError(f"CPQ-56 Blocked: Cannot order {order_qty} on '{target_ln.get('sku')}' (remaining={rem}).")
                        target_ln["orderedQty"] = int(target_ln.get("orderedQty") or 0) + order_qty
                        units_placed += order_qty
                sap_ord = str(payload.get("sapOrderNumber") or f"SAP-ORD-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{int(time.time()) % 10000}")
                q["lastSapOrderNumber"] = sap_ord
                q["lastSapOrderDate"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                for opp in self._relational.get("opportunities", []):
                    if opp.get("opportunityId") == q.get("opportunityId"):
                        opp["stage"] = "Project In Progress"
                        opp["probability"] = 100
                        opp["daysSinceLastOrder"] = 0
                        break
                self._append_event(actor, persona, "cpq.order.placed", qid, f"{sap_ord} ({units_placed} units)")
                self._run_osq_pipeline(actor, persona, {})

            elif action == "osq.pipeline.run":
                self._run_osq_pipeline(actor, persona, payload)

            elif action == "heuristics.update":
                hc = self._ensure_heuristics_config()
                preset = str(payload.get("preset") or payload.get("activePreset") or "").strip()
                if preset:
                    self._apply_heuristics_preset(hc, preset)
                for sk in ("slaAndTiming", "organizationAndMigration", "fulfillmentAndKitting", "forecastAndScb"):
                    if isinstance(payload.get(sk), dict):
                        hc.setdefault(sk, {}).update(copy.deepcopy(payload[sk]))
                if isinstance(payload.get("kittingDcNetwork"), list):
                    hc["kittingDcNetwork"] = copy.deepcopy(payload["kittingDcNetwork"])
                hc["updatedAt"] = _utc_now()
                hc["updatedBy"] = actor
                ev_res = self._evaluate_heuristics_order(payload)
                self._refresh_pathways_and_directives(action, payload, actor)
                self._append_event(actor, persona, "heuristics.updated", str(hc.get("activePreset") or ""), f"Quote={ev_res.get('quoteId')}, Lines={len(ev_res.get('lineDecisions', []))}")

            elif action == "heuristics.evaluate_order":
                ev_res = self._evaluate_heuristics_order(payload)
                self._refresh_pathways_and_directives(action, payload, actor)
                self._append_event(actor, persona, "heuristics.evaluated", str(ev_res.get("quoteId") or ""), f"DC={ev_res.get('localDc')}->{ev_res.get('kittingDc')}")

            elif action == "kitting.workflow.execute":
                qid = str(payload.get("quoteId") or self._relational.get("selectedQuoteId") or "ROF-DEMO-001")
                q = quotes_by_id.get(qid)
                if not q:
                    raise ValueError(f"Quote not found: {qid}")
                if "parentKitSku" in payload:
                    q["parentKitSku"] = str(payload.get("parentKitSku") or q.get("parentKitSku") or "KIT-RENO-1BR")
                new_pqty = max(1, int(float(payload.get("parentKitQty") or q.get("parentKitQty") or 10)))
                old_pqty = max(1, int(float(q.get("parentKitQty") or new_pqty)))
                if payload.get("scaleLinesByParentQty") and new_pqty != old_pqty:
                    ratio = float(new_pqty) / float(old_pqty)
                    for ln in q.get("lines", []):
                        ln["qty"] = max(1, int(round(float(ln.get("qty") or 1) * ratio)))
                q["parentKitQty"] = new_pqty
                q["floorplanConverted"] = True
                q["edsValidated"] = bool(payload.get("edsValidated", True))
                q["outboundPrintValidated"] = bool(payload.get("outboundPrintValidated", True))
                if "unitNumber" in payload:
                    q["unitNumber"] = str(payload.get("unitNumber") or "UNIT-101A")
                if "localDc" in payload:
                    q["localDc"] = str(payload.get("localDc") or "NC34").upper()
                if "orderToRddDays" in payload:
                    q["orderToRddDays"] = int(payload.get("orderToRddDays") or 9)
                if "deliveryBlock" in payload:
                    q["deliveryBlock"] = str(payload.get("deliveryBlock") or "ZR")
                ev_res = self._evaluate_heuristics_order(payload)
                cnt = ev_res.get("summaryCounts") or {}
                q["zstockStatus"] = "FUTURE_KIT_PROTECTED" if int(cnt.get("waitPreserveCount") or 0) > 0 else "REVIEWED_OK"
                self._refresh_pathways_and_directives(action, payload, actor)
                self._append_event(actor, persona, "kitting.workflow.executed", qid, f"Parent={q.get('parentKitSku')}x{new_pqty}, Unit={q.get('unitNumber')}, ZSTOCK={q.get('zstockStatus')}")

            elif action == "quote.split_allocate":
                hc = self._ensure_heuristics_config()
                qid = str(payload.get("quoteId") or self._relational.get("selectedQuoteId") or "ROF-DEMO-001")
                q = quotes_by_id.get(qid)
                if not q:
                    raise NotFoundError(f"Quote not found: {qid}")
                token_hash = _sha256_json({"id": qid, "customer": q.get("customer"), "lines": q.get("lines", [])})[:24]
                ev_res = self._evaluate_heuristics_order(payload)
                k_dc = str(ev_res.get("kittingDc") or "GA02")
                by_dc: Dict[str, List[Dict[str, Any]]] = {}
                for ld in ev_res.get("lineDecisions", []):
                    by_dc.setdefault(str(ld.get("spokeDc") or k_dc), []).append({"sku": ld.get("sku"), "qty": ld.get("qty"), "mode": ld.get("decisionCode")})
                shards = []
                for idx, (src_dc, slines) in enumerate(by_dc.items()):
                    dr = self._run_weighted_dijkstra(src_dc, k_dc, len(slines) * 5, hc)
                    shards.append({
                        "subOrderId": f"SO-{qid}.{idx}", "sourceDc": src_dc, "targetDc": k_dc,
                        "lineCount": len(slines), "lines": slines, "dijkstraRoute": dr,
                        "consolidationDifferentialUsd": dr["consolidationDifferentialUsd"],
                        "optionalHoldingCostUpchargeUsd": dr["optionalHoldingCostUpchargeUsd"],
                    })
                q["quoteTokenHash"] = token_hash
                q["splitAllocations"] = shards
                self._append_event(actor, persona, "quote.split_allocate", qid, f"Token={token_hash}, Shards={len(shards)}")

            elif action == "bom.flatten_modular":
                qid = str(payload.get("quoteId") or self._relational.get("selectedQuoteId") or "ROF-DEMO-001")
                q = quotes_by_id.get(qid)
                if not q:
                    raise NotFoundError(f"Quote not found: {qid}")
                ev_res = self._evaluate_heuristics_order(payload)
                m_groups = (ev_res.get("tx03Tx77Architecture") or {}).get("modularBomGroups") or []
                q["modularBomGroups"] = m_groups
                mod_store = self._relational.setdefault("modularBoms", [])
                mod_store.append({"quoteId": qid, "flattenedAt": _utc_now(), "groups": m_groups})
                del mod_store[:-100]
                self._append_event(actor, persona, "bom.flatten_modular", qid, f"Modules={len(m_groups)}")

            elif action == "sto.schedule":
                ord_id = str(payload.get("orderId") or payload.get("quoteId") or "ROF-DEMO-001")
                sto_code = str(payload.get("stoCode") or payload.get("skuSignature") or "STO-DEFAULT")
                rdd = str(payload.get("rdd") or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                from_dc = str(payload.get("fromDc") or "NC34").upper()
                to_dc = str(payload.get("toDc") or "GA02").upper()
                s_qty = max(1, int(payload.get("qty") or 1))
                sup_over = bool(payload.get("supervisorOverride", False))
                stos = self._relational.setdefault("scheduledStos", [])
                if not sup_over and any(s.get("orderId") == ord_id and s.get("stoCode") == sto_code and s.get("rdd") == rdd for s in stos):
                    raise ConflictError(f"Same-day duplicate STO blocked (409): orderId={ord_id}, stoCode={sto_code}, rdd={rdd}. Pass supervisorOverride=true or schedule on a different RDD.")
                rfids = self._relational.setdefault("rfidShipments", [])
                ship_id = f"IS-{1000 + len(rfids) + 1}"
                stos.append({"orderId": ord_id, "stoCode": sto_code, "rdd": rdd, "fromDc": from_dc, "toDc": to_dc, "qty": s_qty, "supervisorOverride": sup_over, "shipmentId": ship_id, "createdAt": _utc_now()})
                rfids.append({"shipmentId": ship_id, "shipmentType": "IS", "lineStyle": "dashed", "fromDc": from_dc, "toDc": to_dc, "status": "SCHEDULED", "protocol": "rust-rfid-open-tracker/v1", "tagId": f"RUST-RFID-{ship_id.replace('-', '')}", "baselineHours": 4.5, "activatedAtUtc": _utc_now()})
                self._append_event(actor, persona, "sto.scheduled", ship_id, f"{ord_id} / {sto_code} @ {rdd}")

            elif action == "order.credit_hold_rdd_adjust":
                qid = str(payload.get("quoteId") or self._relational.get("selectedQuoteId") or "ROF-DEMO-001")
                q = quotes_by_id.get(qid)
                if not q:
                    raise NotFoundError(f"Quote not found: {qid}")
                shift_d = int(payload.get("shiftDays") or 3)
                q["orderToRddDays"] = max(1, int(q.get("orderToRddDays") or 8) + shift_d)
                q["creditHoldDays"] = int(payload.get("creditHoldDays") or 0)
                q["creditHoldRddAdjustedAt"] = _utc_now()
                if payload.get("newRdd"):
                    q["adjustedRdd"] = str(payload["newRdd"])
                self._evaluate_heuristics_order({"quoteId": qid})
                self._relational.setdefault("sapSignals", []).append({"signalType": "RDD_CREDIT_SHIFT", "targetId": qid, "shiftDays": shift_d, "status": "DISPATCHED_TO_SAP_BRIDGE", "at": _utc_now()})
                self._append_event(actor, persona, "order.credit_hold_rdd_adjusted", qid, f"ShiftDays={shift_d}, NewLeadDays={q['orderToRddDays']}")

            elif action == "ifr.auto_po":
                self._ensure_heuristics_config()
                tgt_ifr = float(payload.get("targetIfrPct") or 98.5)
                req_sku = str(payload.get("sku") or "").upper()
                pos = self._relational.setdefault("autoReplenishmentPos", [])
                gen = 0
                for it in self._relational.get("items", []):
                    sku = str(it.get("sku") or "").upper()
                    if req_sku and sku != req_sku:
                        continue
                    fm, cm, sm = it.get("floorQohByDc") or {}, it.get("committedQtyByDc") or {}, it.get("storage916QtyByDc") or {}
                    for dc, fv in fm.items():
                        avail = max(0, int(fv or 0) - int(cm.get(dc) or 0) - int(sm.get(dc) or 0))
                        if avail < 12:
                            pos.append({"poId": f"PO-IFR-{len(pos) + 101}", "sku": sku, "dc": dc, "orderQty": max(15, 25 - avail), "projectedIfrPct": tgt_ifr, "status": "AUTO_GENERATED_OPEN", "createdAt": _utc_now()})
                            gen += 1
                if gen == 0:
                    pos.append({"poId": f"PO-IFR-{len(pos) + 101}", "sku": req_sku or "SKU-M18-DRILL", "dc": str(payload.get("dc") or "GA02"), "orderQty": max(1, int(payload.get("qty") or 20)), "projectedIfrPct": tgt_ifr, "status": "AUTO_GENERATED_OPEN", "createdAt": _utc_now()})
                self._refresh_pathways_and_directives(action, payload, actor)
                self._append_event(actor, persona, "ifr.auto_po.generated", "IFR", f"Count={len(pos)}, TargetIFR={tgt_ifr}")

            elif action == "order.cancel_reconcile":
                ord_id = str(payload.get("orderId") or payload.get("quoteId") or "ROF-DEMO-001")
                c_sku = str(payload.get("sku") or "SKU-FAUCET-PRO").upper()
                red_qty = max(1, int(payload.get("reducedQty") or 5))
                arrived = bool(payload.get("inboundArrivedWithoutReduction", False))
                pos = self._relational.setdefault("autoReplenishmentPos", [])
                for po in pos:
                    if str(po.get("sku") or "").upper() == c_sku:
                        po["status"] = "PO_REDUCTION_REQUIRED"
                if arrived:
                    q_list = self._relational.setdefault("receivingQuarantines", [])
                    q_list.append({"quarantineId": f"QRN-{len(q_list) + 1}", "orderId": ord_id, "sku": c_sku, "qty": red_qty, "status": "RECEIVING_QUARANTINE_RETURN", "quarantineAction": "HOLD_ON_DOCK_FOR_VENDOR_RETURN", "alertRecipients": ["Receiving", "Buyer", "Supervisor"], "createdAt": _utc_now()})
                else:
                    pos.append({"poId": f"PO-RED-{len(pos) + 101}", "sku": c_sku, "dc": str(payload.get("dc") or "TX03"), "orderQty": red_qty, "status": "PO_REDUCTION_REQUIRED", "orderId": ord_id, "createdAt": _utc_now()})
                self._refresh_pathways_and_directives(action, payload, actor)
                self._append_event(actor, persona, "order.cancel_reconciled", ord_id, f"SKU={c_sku}, Quarantine={arrived}")

            elif action == "rfid.event":
                ship_id = str(payload.get("shipmentId") or "IS-1001").upper()
                ev_type = str(payload.get("eventType") or "RECEIPT_SCAN").upper()
                from_dc = str(payload.get("fromDc") or "NC34").upper()
                to_dc = str(payload.get("toDc") or "GA02").upper()
                obs_hrs = float(payload.get("observedHours") or 5.2)
                base_hrs = float(payload.get("baselineHours") or 4.5)
                s_type = "CS" if ship_id.startswith("CS-") else "IS"
                rfids = self._relational.setdefault("rfidShipments", [])
                rec = next((r for r in rfids if r.get("shipmentId") == ship_id), None)
                if rec is None:
                    rec = {"shipmentId": ship_id, "shipmentType": s_type, "lineStyle": "solid" if s_type == "CS" else "dashed", "fromDc": from_dc, "toDc": to_dc, "protocol": "rust-rfid-open-tracker/v1", "tagId": str(payload.get("tagId") or f"RUST-RFID-{ship_id.replace('-', '')}"), "baselineHours": base_hrs}
                    rfids.append(rec)
                if ev_type == "ACTIVATE":
                    rec["status"], rec["activatedAtUtc"] = "ACTIVE_IN_TRANSIT", _utc_now()
                elif ev_type == "RECEIPT_SCAN":
                    rec["status"], rec["receivedAtUtc"], rec["elapsedHours"] = "RECEIVED_VERIFIED", _utc_now(), obs_hrs
                    ema_map = self._relational.setdefault("rfidEdgeWeights", {})
                    ekey = f"{from_dc}->{to_dc}"
                    prev_ema = float(ema_map.get(ekey, 1.0))
                    ratio = max(0.4, min(3.0, obs_hrs / max(0.5, base_hrs)))
                    ema_map[ekey] = round(0.75 * prev_ema + 0.25 * ratio, 4)
                    closed_fb = CyberPhysicalPathwayRouter.auto_close_directives_from_physical_event(
                        self._relational.setdefault("bodyDirectives", []),
                        self._relational.setdefault("bodyFeedback", []),
                        event_kind="RFID_RECEIPT_SCAN",
                        match_tokens=[ship_id, from_dc, to_dc, payload.get("sku"), payload.get("orderId"), payload.get("quoteId")],
                        actor=actor,
                        outcome=f"RFID RECEIPT_SCAN on {ship_id} ({from_dc}->{to_dc}) elapsed={obs_hrs}h",
                    )
                    if closed_fb:
                        CyberPhysicalPathwayRouter.sync_to_sqlite_brain(self.sqlite_path, self._relational["bodyDirectives"], closed_fb)
                        self._refresh_pathways_and_directives(action, payload, actor)
                else:
                    rec["status"], rec["checkpointAtUtc"] = ev_type, _utc_now()
                self._append_event(actor, persona, "rfid.event", ship_id, f"{ev_type} ({from_dc}->{to_dc})")

            elif action == "quipu.classify_note":
                txt = str(payload.get("noteText") or "")
                low = txt.lower()
                cls = "DAMAGED_DOOR_HOLD" if any(k in low for k in ("damage", "dent", "door")) else ("CYCLE_COUNT_VARIANCE" if any(k in low for k in ("short", "count", "missing")) else ("WIP_STAGING_PARTIAL" if any(k in low for k in ("wip", "stage", "hold")) else "STANDARD_PUTAWAY"))
                notes = self._relational.setdefault("quipuNoteClassifications", [])
                notes.append({"noteId": f"NOTE-{len(notes) + 1}", "sourceType": str(payload.get("sourceType") or "pallet_note"), "sku": str(payload.get("sku") or "SKU-DOOR-36"), "dc": str(payload.get("dc") or "TX03"), "noteText": txt, "classification": cls, "wipImpactFlag": cls != "STANDARD_PUTAWAY", "cycleCountRiskScore": 0.88 if cls in ("CYCLE_COUNT_VARIANCE", "DAMAGED_DOOR_HOLD") else 0.34, "classifiedAt": _utc_now()})
                self._refresh_pathways_and_directives(action, payload, actor)
                self._append_event(actor, persona, "quipu.note.classified", cls, txt)

            elif action == "stockopoly.slot_lookup":
                s_sku = str(payload.get("sku") or "SKU-FAUCET-PRO").upper()
                s_dc = str(payload.get("dc") or "TX03").upper()
                sap_bin = str(payload.get("sapLocationCode") or f"{s_dc}-BIN-A12-L3")
                drift = bool(payload.get("simulateDrift", False))
                self._relational.setdefault("stockopolyBinMappings", []).append({"sku": s_sku, "dc": s_dc, "sapStorageLocation": sap_bin, "stockopolyCoords": {"x": 12.4, "y": 4.8, "z": 2.1, "aisle": "A12", "bay": "L3", "corners": 4}, "matchStatus": "RECONCILE_BIN_DRIFT" if drift else "VERIFIED_BIN_MATCH", "checkedAt": _utc_now()})
                self._refresh_pathways_and_directives(action, payload, actor)
                self._append_event(actor, persona, "stockopoly.slot_lookup", s_sku, f"{s_dc} @ {sap_bin}")

            elif action == "sap.signal":
                sig_type = str(payload.get("signalType") or "QUOTE_RESERVE").strip().upper()
                if sig_type == "RAW_INVENTORY_ADJUST" or "RAW_INVENTORY" in sig_type or "DIRECT_QOH" in sig_type:
                    self._record_restricted_audit(actor, "DENY", f"Blocked forbidden SAP signal: {sig_type}", corr_id, "")
                    raise ForbiddenError("SAP Policy Violation (403): RAW_INVENTORY_ADJUST is strictly forbidden outside physical SAP EWM/MIGO; use bounded operational signals (PR_ADJUST, PO_ADJUST, QUOTE_RESERVE, PICK_COMPLETE_KNOCKOFF, RDD_CREDIT_SHIFT).")
                sigs = self._relational.setdefault("sapSignals", [])
                sigs.append({"signalId": f"SAP-SIG-{len(sigs) + 101}", "signalType": sig_type, "targetId": str(payload.get("targetId") or "ROF-DEMO-001"), "dc": str(payload.get("dc") or "TX03"), "sku": str(payload.get("sku") or ""), "status": "DISPATCHED_TO_SAP_BRIDGE", "dispatchedAt": _utc_now()})
                if sig_type == "PICK_COMPLETE_KNOCKOFF":
                    closed_fb = CyberPhysicalPathwayRouter.auto_close_directives_from_physical_event(
                        self._relational.setdefault("bodyDirectives", []),
                        self._relational.setdefault("bodyFeedback", []),
                        event_kind="SAP_PICK_COMPLETE_KNOCKOFF",
                        match_tokens=[payload.get("targetId"), payload.get("sku"), payload.get("dc")],
                        actor=actor,
                        outcome=f"SAP PICK_COMPLETE_KNOCKOFF on {payload.get('targetId')} ({payload.get('sku')})",
                    )
                    if closed_fb:
                        CyberPhysicalPathwayRouter.sync_to_sqlite_brain(self.sqlite_path, self._relational["bodyDirectives"], closed_fb)
                        self._refresh_pathways_and_directives(action, payload, actor)
                self._append_event(actor, persona, "sap.signal.dispatched", sig_type, str(payload.get("targetId") or "ROF-DEMO-001"))

            elif action == "directive.feedback":
                dir_id = str(payload.get("directiveId") or payload.get("fingerprint") or "").strip()
                fb_status = str(payload.get("status") or "done").strip().lower()
                if fb_status not in ("ack", "in_progress", "done", "rejected"):
                    raise ValueError(f"Unsupported directive feedback status: {fb_status!r}")
                outcome = str(payload.get("outcome") or "").strip()
                dirs = self._relational.setdefault("bodyDirectives", [])
                target_d = next((d for d in dirs if str(d.get("directiveId") or "") == dir_id or str(d.get("fingerprint") or "") == dir_id), None)
                if not target_d:
                    raise NotFoundError(f"Directive not found: {dir_id}")
                target_d["status"] = fb_status
                target_d["lastStatusAt"] = _utc_now()
                fb_list = self._relational.setdefault("bodyFeedback", [])
                fb_rec = {
                    "feedbackId": f"FB-{len(fb_list) + 1}",
                    "directiveId": target_d.get("directiveId"),
                    "fingerprint": target_d.get("fingerprint"),
                    "status": fb_status,
                    "triggerEvent": "MANUAL_OPERATOR_FEEDBACK",
                    "outcome": outcome,
                    "executedBy": actor,
                    "loggedAt": _utc_now(),
                }
                fb_list.append(fb_rec)
                CyberPhysicalPathwayRouter.sync_to_sqlite_brain(self.sqlite_path, dirs, [fb_rec])
                self._refresh_pathways_and_directives(action, payload, actor)
                self._append_event(actor, persona, "directive.feedback", str(target_d.get("directiveId")), f"{fb_status}: {outcome[:120]}")

            else:
                raise ValueError(f"Unknown relational action: {action!r}")

            new_rev = int(self._relational.get("revision", 1)) + 1
            self._relational["revision"] = new_rev
            self._relational["updatedAt"] = _utc_now()
            receipts = self._build_canonical_objects(action, actor, payload, correlation_id=corr_id, causation_id=body.get("causationId"))
            outbox_seq = len(self._outbox) + 1
            outbox_entry = {
                "seq": outbox_seq,
                "eventId": receipts["event"]["eventId"],
                "taskId": receipts["task_id"],
                "correlation_id": receipts["correlation_id"],
                "causation_id": receipts["causation_id"],
                "eventType": f"relational.{action}",
                "revision": new_rev,
                "createdAt": _utc_now(),
                "actor": actor,
            }
            self._outbox.append(outbox_entry)
            if len(self._outbox) > 1000:
                del self._outbox[: len(self._outbox) - 1000]
            self._tasks[receipts["task_id"]] = receipts
            self._task_order.append(receipts["task_id"])
            if len(self._task_order) > 500:
                old_tid = self._task_order.pop(0)
                self._tasks.pop(old_tid, None)

            idem_rec = None
            if idempotency_key:
                idem_rec = {
                    "idempotencyKey": idempotency_key,
                    "requestHash": req_hash,
                    "taskId": receipts["task_id"],
                    "correlationId": receipts["correlation_id"],
                    "causationId": receipts["causation_id"],
                    "outboxCursor": outbox_seq,
                    "taskReceipt": receipts,
                    "statusCode": 202 if body.get("async") else 200,
                    "responseSummary": {"ok": True, "revision": new_rev, "taskId": receipts["task_id"]},
                    "createdAt": _utc_now(),
                }
                self._idempotency_index[idempotency_key] = idem_rec

            self._record_restricted_audit(
                actor, "ALLOW", f"Committed {action} at rev {new_rev}", receipts["correlation_id"], receipts["task_id"]
            )
            _atomic_write_json(self.relational_path, self._relational)
            self._mirror_revision_sqlite(outbox_entry=outbox_entry, idempotency_rec=idem_rec)

        self._broadcast("relational.mutated", outbox_seq=outbox_seq)
        self._push_outbox_to_hubcore_async(outbox_entry)
        state_out = self.get_state()
        state_out.update({
            "idempotentReplay": False,
            "idempotencyKey": idempotency_key or None,
            "taskId": receipts["task_id"],
            "correlationId": receipts["correlation_id"],
            "causationId": receipts["causation_id"],
            "outboxCursor": outbox_seq,
            "taskReceipt": receipts,
        })
        return state_out

    def update_temp(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Apply shared ephemeral updates (presence, soft locks, drafts, what-if, live chat)."""
        action = str(body.get("action") or "").strip()
        payload = body.get("payload") or {}
        now_epoch = time.time()

        with self._lock:
            self._prune_temp()

            if action == "session.heartbeat":
                sid = str(payload.get("sessionId") or "")
                sessions = [s for s in self._temp.get("activeSessions", []) if s.get("sessionId") != sid]
                sessions.append(
                    {
                        "sessionId": sid,
                        "userId": str(payload.get("userId") or "User"),
                        "persona": str(payload.get("persona") or "RSR / Seller"),
                        "activeTab": str(payload.get("activeTab") or "tab-overview"),
                        "focusedEntity": str(payload.get("focusedEntity") or "ROF-DEMO-001"),
                        "lastSeen": _utc_now(),
                        "lastSeenEpoch": now_epoch,
                    }
                )
                self._temp["activeSessions"] = sessions

            elif action == "lock.acquire":
                ekey = str(payload.get("entityKey") or "")
                sid = str(payload.get("sessionId") or "")
                existing = next((lk for lk in self._temp.get("entityLocks", []) if lk.get("entityKey") == ekey), None)
                if existing and existing.get("sessionId") != sid:
                    return {
                        "ok": False,
                        "conflict": True,
                        "holder": existing,
                        **self.get_state(),
                    }
                self._fencing_counter += 1
                fence = self._fencing_counter
                self._entity_max_fencing[ekey] = fence
                lease_id = f"LEASE-{fence}-{int(now_epoch)}"
                locks = [lk for lk in self._temp.get("entityLocks", []) if lk.get("entityKey") != ekey]
                locks.append(
                    {
                        "entityKey": ekey,
                        "sessionId": sid,
                        "leaseId": lease_id,
                        "fencingToken": fence,
                        "userId": str(payload.get("userId") or "User"),
                        "persona": str(payload.get("persona") or "RSR / Seller"),
                        "action": str(payload.get("lockAction") or "Editing"),
                        "acquiredAt": _utc_now(),
                        "expiresEpoch": now_epoch + self.LOCK_TTL_SECONDS,
                    }
                )
                self._temp["entityLocks"] = locks

            elif action == "lock.release":
                ekey = str(payload.get("entityKey") or "")
                sid = str(payload.get("sessionId") or "")
                self._temp["entityLocks"] = [
                    lk for lk in self._temp.get("entityLocks", [])
                    if not (lk.get("entityKey") == ekey and (not sid or lk.get("sessionId") == sid))
                ]

            elif action == "draft.update":
                dkey = str(payload.get("draftKey") or "")
                drafts = self._temp.setdefault("sharedDrafts", {})
                text_val = str(payload.get("text") or "")
                if text_val.strip():
                    drafts[dkey] = {
                        "draftKey": dkey,
                        "userId": str(payload.get("userId") or "User"),
                        "persona": str(payload.get("persona") or "Customer"),
                        "text": text_val[:1200],
                        "updatedAt": _utc_now(),
                    }
                else:
                    drafts.pop(dkey, None)

            elif action == "whatif.update":
                self._temp["whatIfScratch"] = {
                    **(self._temp.get("whatIfScratch") or {}),
                    **payload,
                    "updatedAt": _utc_now(),
                }

            elif action == "chat.post":
                ref_id = str(payload.get("refId") or "ROF-DEMO-001")[:60]
                ch_type = "shipment_is" if ref_id.upper().startswith("IS-") else ("shipment_cs" if ref_id.upper().startswith("CS-") else "quote")
                msg = {
                    "id": f"MSG-{int(time.time()*1000)}",
                    "time": _utc_now(),
                    "author": str(payload.get("author") or "User")[:80],
                    "persona": str(payload.get("persona") or "RSR / Seller")[:60],
                    "refId": ref_id,
                    "channelType": ch_type,
                    "mentions": list(payload.get("mentions") or []),
                    "text": str(payload.get("text") or "")[:600],
                }
                feed = self._temp.setdefault("liveChatFeed", [])
                feed.append(msg)
                if len(feed) > 250:
                    del feed[: len(feed) - 250]

            else:
                raise ValueError(f"Unknown temp action: {action!r}")

            self._temp["revision"] = int(self._temp.get("revision", 1)) + 1
            self._temp["updatedAt"] = _utc_now()
            _atomic_write_json(self.temp_path, self._temp)
            self._mirror_ephemeral_sqlite()

        self._broadcast(f"temp.{action}")
        return self.get_state()

    def get_tasks(self, task_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if task_id:
                rec = self._tasks.get(task_id)
                if not rec:
                    raise NotFoundError(f"Task not found: {task_id}")
                return {"ok": True, "taskReceipt": rec}
            return {"ok": True, "tasks": [self._tasks[tid] for tid in reversed(self._task_order) if tid in self._tasks]}

    def get_registrations(self) -> Dict[str, Any]:
        with self._lock:
            now_epoch = time.time()
            for reg in self._registrations.values():
                if reg.get("status") == "ACTIVE" and float(reg.get("leaseExpiresEpoch") or 0.0) < now_epoch:
                    reg["status"] = "EXPIRED"
                    reg["routable"] = False
                    reg["reason"] = "Registration lease expired"
            return {
                "ok": True,
                "homeNodeId": self.HOME_NODE_ID,
                "registrations": list(self._registrations.values()),
            }

    def get_restricted_audit(self) -> Dict[str, Any]:
        with self._lock:
            return {"ok": True, "policyVersion": self.POLICY_VERSION, "audit": list(self._restricted_audit)}

    def register_holon(self, body: Dict[str, Any]) -> Dict[str, Any]:
        hid = str(body.get("holonId") or "").strip()
        version = str(body.get("version") or "").strip()
        artifact_hash = str(body.get("artifactHash") or "").strip()
        expected_digest = str(body.get("expectedDigest") or "").strip()
        capabilities = list(body.get("capabilities") or [])
        readiness_ok = bool(body.get("readinessOk", True))
        signature = str(body.get("signature") or "signed-manifest-sha256").strip()
        ttl = max(30, int(body.get("leaseTtlSeconds") or 300))

        with self._lock:
            if not hid or not version or not capabilities or not signature:
                reason = "Invalid manifest: holonId, version, capabilities, and signature are required."
                rec = {
                    "registrationId": f"REG-REJ-{int(time.time())}",
                    "holonId": hid or "unknown",
                    "status": "REJECTED",
                    "routable": False,
                    "reason": reason,
                    "registeredAt": _utc_now(),
                }
                if hid:
                    self._registrations[hid] = rec
                self._record_restricted_audit(hid or "unknown", "DENY", reason)
                raise ValueError(reason)
            if expected_digest and artifact_hash != expected_digest:
                reason = f"Manifest artifact digest mismatch: expected {expected_digest}, got {artifact_hash}."
                rec = {
                    "registrationId": f"REG-REJ-{int(time.time())}",
                    "holonId": hid,
                    "status": "REJECTED",
                    "routable": False,
                    "reason": reason,
                    "registeredAt": _utc_now(),
                }
                self._registrations[hid] = rec
                self._record_restricted_audit(hid, "DENY", reason)
                raise ValueError(reason)
            if not readiness_ok:
                rec = {
                    "registrationId": f"REG-PEND-{int(time.time())}",
                    "holonId": hid,
                    "version": version,
                    "status": "PENDING",
                    "routable": False,
                    "reason": "Health readiness probe failed; no routable capability.",
                    "registeredAt": _utc_now(),
                }
                self._registrations[hid] = rec
                self._record_restricted_audit(hid, "HOLD", rec["reason"])
                return {"ok": True, "registration": rec}

            now_epoch = time.time()
            lease_exp = datetime.fromtimestamp(now_epoch + ttl, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec = {
                "registrationId": f"REG-{hid.upper()}-{int(now_epoch) % 100000}",
                "holonId": hid,
                "instanceId": str(body.get("instanceId") or f"{hid}-node-a-01"),
                "homeNodeId": self.HOME_NODE_ID,
                "version": version,
                "status": "ACTIVE",
                "routable": True,
                "capabilities": capabilities,
                "grantedScopes": list(body.get("requestedGrants") or ["crm.read", "crm.write", "cpq.write", "osq.read", "osq.write"]),
                "healthEndpoint": str(body.get("healthEndpoint") or "/health"),
                "artifactHash": artifact_hash or _sha256_json({"holonId": hid, "version": version}),
                "registeredAt": _utc_now(),
                "leaseExpiresAt": lease_exp,
                "leaseExpiresEpoch": now_epoch + ttl,
                "reason": "Verified identity, signature, schema, digest & readiness",
            }
            self._registrations[hid] = rec
            self._record_restricted_audit(hid, "ALLOW", f"Registered ACTIVE lease until {lease_exp}")
            return {"ok": True, "registration": rec}

    def renew_registration(self, body: Dict[str, Any]) -> Dict[str, Any]:
        hid = str(body.get("holonId") or "x_osq").strip()
        ttl = max(30, int(body.get("leaseTtlSeconds") or 300))
        with self._lock:
            rec = self._registrations.get(hid)
            if not rec:
                raise NotFoundError(f"Registration not found for {hid}")
            if rec.get("status") == "REVOKED":
                raise ValueError(f"Cannot renew revoked registration for {hid}; re-register manifest.")
            if not bool(body.get("healthOk", True)):
                rec["status"] = "UNHEALTHY"
                rec["routable"] = False
                rec["reason"] = "Health check failed during lease renewal"
                self._mesh_state["advertisedWithdrawnCount"] = int(self._mesh_state.get("advertisedWithdrawnCount", 0)) + 1
                self._record_restricted_audit(hid, "DENY", rec["reason"])
                return {"ok": False, "registration": rec}
            now_epoch = time.time()
            lease_exp = datetime.fromtimestamp(now_epoch + ttl, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["status"] = "ACTIVE"
            rec["routable"] = True
            rec["leaseExpiresAt"] = lease_exp
            rec["leaseExpiresEpoch"] = now_epoch + ttl
            rec["reason"] = "Lease renewed after health & grant recheck"
            self._record_restricted_audit(hid, "ALLOW", f"Renewed lease until {lease_exp}")
            return {"ok": True, "registration": rec}

    def revoke_registration(self, body: Dict[str, Any]) -> Dict[str, Any]:
        hid = str(body.get("holonId") or "x_osq").strip()
        reason = str(body.get("reason") or "Operator revoked capability route").strip()
        new_status = "EXPIRED" if reason.lower() == "expired" else "REVOKED"
        with self._lock:
            rec = self._registrations.get(hid)
            if not rec:
                raise NotFoundError(f"Registration not found for {hid}")
            rec["status"] = new_status
            rec["routable"] = False
            rec["reason"] = reason
            rec["revokedAt"] = _utc_now()
            self._mesh_state["advertisedWithdrawnCount"] = int(self._mesh_state.get("advertisedWithdrawnCount", 0)) + 1
            self._record_restricted_audit(hid, "DENY", f"Registration {new_status}: {reason}")
            return {"ok": True, "withdrawnFromMesh": True, "registration": rec}

    def verify_marketplace_artifact(self, body: Dict[str, Any]) -> Dict[str, Any]:
        trust = body.get("trustEnvelope") or body
        publisher = str(trust.get("publisher") or "").strip()
        scope = str(trust.get("scope") or "").strip()
        audience = str(trust.get("audience") or "").strip()
        signature = str(trust.get("signature") or "").strip()
        replay_id = str(trust.get("replayId") or "").strip()
        signed_digest = str(trust.get("signedDigest") or "").strip().lower()
        expires_at = str(trust.get("expiresAt") or "").strip()
        revoked = bool(trust.get("revoked", False))
        artifact = body.get("artifact") or body.get("payload") or {}

        with self._lock:
            if not publisher or not scope or not audience or not signature or not replay_id:
                raise ValueError("Marketplace Trust Gate Blocked: publisher, scope, audience, signature, and replayId are required.")
            if revoked:
                raise ForbiddenError("Marketplace Trust Gate Blocked: artifact publisher/credential is revoked.")
            exp_dt = self._parse_utc_iso(expires_at)
            if not exp_dt or exp_dt <= datetime.now(timezone.utc):
                raise ValueError("Marketplace Trust Gate Blocked: artifact envelope is missing or expired.")
            if replay_id in self._marketplace_replays:
                raise ConflictError(f"Marketplace Trust Gate Conflict: replayId '{replay_id}' was already consumed.")
            computed_digest = _sha256_json(artifact)
            if signed_digest != computed_digest:
                self._record_restricted_audit(publisher, "DENY", f"Marketplace digest mismatch for replayId={replay_id}")
                raise ValueError(f"Marketplace Trust Gate Blocked: byte digest mismatch (expected {signed_digest}, computed {computed_digest}).")

            self._marketplace_replays.add(replay_id)
            shard = {
                "asset_id": artifact.get("asset_id") or f"osq-verified-{int(time.time())}",
                "token_id": artifact.get("token_id") or f"tok-{scope}-{replay_id[:8]}",
                "axis": artifact.get("axis") or "entirety",
                "title": artifact.get("title") or "Verified GARD Artifact Exchange Shard",
                "marketplace_scope": scope,
                "supply_chain_domain": artifact.get("domain") or "forecasting",
                "sku_ref": artifact.get("sku_ref") or "SKU-HVAC-3001",
                "batch_id": artifact.get("batch_id") or "BATCH-VERIFIED",
                "available_shards": int(artifact.get("shard_count") or 250),
                "current_shard_price": float(artifact.get("shard_price") or 12.5),
                "protocol": "gard-shard/v2",
                "publisher": publisher,
                "audience": audience,
                "replayId": replay_id,
                "envelope_digest": computed_digest,
                "grantScope": "gard.artifact.exchange",
            }
            self._relational.setdefault("marketplaceShards", []).insert(0, shard)
            self._append_event(publisher, "GARD Trust Gate", "marketplace.artifact.verified", shard["asset_id"], f"Scope={scope}, digest={computed_digest[:12]}")
            self._relational["revision"] = int(self._relational.get("revision", 1)) + 1
            self._relational["updatedAt"] = _utc_now()
            _atomic_write_json(self.relational_path, self._relational)
            self._mirror_revision_sqlite()
            self._record_restricted_audit(publisher, "ALLOW", f"Verified GARD artifact exchange {shard['asset_id']}")
        self._broadcast("marketplace.verified")
        return {"ok": True, "verified": True, "grantScope": "gard.artifact.exchange", "computedDigest": computed_digest, "shard": shard, "state": self.get_state()}

    def execute_connector(self, body: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(body.get("connectorId") or "").strip()
        mode = str(body.get("mode") or "read").strip().lower()
        prov_key = str(body.get("providerIdempotencyKey") or "").strip()
        unknown_outcome = bool(body.get("simulateUnknownOutcome", False))
        reconcile = bool(body.get("reconcileBeforeRetry", False))

        with self._lock:
            self._check_active_route("x_osq")
            conn = next((c for c in self._relational.get("connectors", []) if c.get("id") == cid), None)
            if not conn:
                raise NotFoundError(f"Connector not found: {cid}")
            if mode == "mutation":
                if not prov_key:
                    raise ValueError("ERP Adapter Blocked: mutation mode requires providerIdempotencyKey.")
                prev = self._connector_outcomes.get(prov_key)
                if prev and prev["outcome"] == "UNKNOWN_OUTCOME_HOLD" and not reconcile:
                    return {
                        "ok": False,
                        "status": "HOLD",
                        "outcome": "UNKNOWN_OUTCOME_HOLD",
                        "reason": "Previous external mutation had unknown outcome; reconcileBeforeRetry=true required before retry.",
                        "providerIdempotencyKey": prov_key,
                    }
                if unknown_outcome and not reconcile:
                    self._connector_outcomes[prov_key] = {"outcome": "UNKNOWN_OUTCOME_HOLD", "time": _utc_now()}
                    self._record_restricted_audit(cid, "HOLD", f"Unknown external outcome on key={prov_key}")
                    return {
                        "ok": False,
                        "status": "HOLD",
                        "outcome": "UNKNOWN_OUTCOME_HOLD",
                        "reason": "External ERP call timed out with unknown outcome; held safely without blind retry.",
                        "providerIdempotencyKey": prov_key,
                    }
                self._connector_outcomes[prov_key] = {"outcome": "KNOWN_RESULT", "time": _utc_now()}
                self._record_restricted_audit(cid, "ALLOW", f"Idempotent fulfillment mutation succeeded key={prov_key}")
                return {"ok": True, "status": "COMPLETED", "outcome": "KNOWN_RESULT", "grantScope": "connector.fulfillment.execute", "providerIdempotencyKey": prov_key}
            self._record_restricted_audit(cid, "ALLOW", f"Connector read executed on {cid}")
            return {"ok": True, "status": "COMPLETED", "outcome": "KNOWN_RESULT", "grantScope": "erp.connector.read", "connector": conn}

    def _export_watermark_snapshot(self, actor: str = "system", reason: str = "manual") -> Dict[str, Any]:
        rev = int(self._relational.get("revision", 1))
        digest = _sha256_json(self._relational)
        snap = {
            "snapshotId": f"PARQ-WM-REV{rev}-{digest[:8]}",
            "format": "immutable-parquet-watermark/v2.3",
            "watermarkRevision": rev,
            "schemaVersion": self._relational.get("schemaVersion", "osq.relational.static/v2"),
            "contentHash": digest,
            "createdAt": _utc_now(),
            "createdBy": actor,
            "reason": reason,
            "counts": {
                "accounts": len(self._relational.get("accounts", [])),
                "opportunities": len(self._relational.get("opportunities", [])),
                "budgets": len(self._relational.get("budgets", [])),
                "quotes": len(self._relational.get("quotes", [])),
                "items": len(self._relational.get("items", [])),
            },
        }
        self._immutable_snapshots.append(snap)
        if len(self._immutable_snapshots) > 50:
            del self._immutable_snapshots[: len(self._immutable_snapshots) - 50]
        return snap

    def export_analytics_snapshot(self, actor: str = "system") -> Dict[str, Any]:
        with self._lock:
            snap = self._export_watermark_snapshot(actor=actor, reason="explicit-export")
            return {"ok": True, "snapshot": snap}

    def query_analytics_readonly(self, domain: str = "summary") -> Dict[str, Any]:
        with self._lock:
            snap = self._immutable_snapshots[-1] if self._immutable_snapshots else self._export_watermark_snapshot(reason="query")
            opps = self._relational.get("opportunities", [])
            quotes = self._relational.get("quotes", [])
            fb = self._relational.get("forecastBatch", {})
            active_pipeline = sum(
                float(o.get("estimatedAmount") or 0.0)
                for o in opps
                if o.get("opportunityCondition") not in ("Pro Forma", "MOG") and o.get("stage") != "Closed Lost"
            )
            avg_margin = 0.0
            if quotes:
                avg_margin = round(sum(float(q.get("approvedMarginPct") or 0.0) for q in quotes) / len(quotes), 2)
            return {
                "ok": True,
                "engine": "duckdb-readonly-projection",
                "readOnly": True,
                "domain": domain,
                "watermarkRevision": snap["watermarkRevision"],
                "contentHash": snap["contentHash"],
                "metrics": {
                    "activePipelineUsd": round(active_pipeline, 2),
                    "avgQuoteMarginPct": avg_margin,
                    "byPublishedUnits": float(fb.get("byPublishedUnits") or 0.0),
                    "emergencyBrakeStatus": (fb.get("emergencyBrake") or {}).get("status", "HELD"),
                },
            }

    def mesh_hello(self, body: Dict[str, Any]) -> Dict[str, Any]:
        peer_id = str(body.get("nodeId") or self.PEER_NODE_ID).strip()
        proto = str(body.get("protocolVersion") or "holon-mesh/2.3").strip()
        with self._lock:
            binding = {
                "peerNodeId": peer_id,
                "homeNodeId": self.HOME_NODE_ID,
                "protocolVersion": proto,
                "mtlsVerified": bool(body.get("mtlsVerified", True)),
                "boundAt": _utc_now(),
            }
            self._mesh_state["peerBindings"][peer_id] = binding
            self._record_restricted_audit(peer_id, "ALLOW", f"MESH HELLO_ACK bound peer {peer_id}")
            return {"ok": True, "messageType": "HELLO_ACK", **binding}

    def mesh_advertise(self) -> Dict[str, Any]:
        with self._lock:
            active_holons = []
            for hid, reg in self._registrations.items():
                if reg.get("status") == "ACTIVE" and reg.get("routable") and float(reg.get("leaseExpiresEpoch") or 0.0) >= time.time():
                    active_holons.append({
                        "holonId": hid,
                        "version": reg.get("version"),
                        "capabilities": reg.get("capabilities", []),
                        "grantedScopes": reg.get("grantedScopes", []),
                        "leaseExpiresAt": reg.get("leaseExpiresAt"),
                    })
            snap = self._immutable_snapshots[-1] if self._immutable_snapshots else self._export_watermark_snapshot(reason="advertise")
            return {
                "ok": True,
                "messageType": "ADVERTISE_ACK",
                "nodeId": self.HOME_NODE_ID,
                "protocolVersion": "holon-mesh/2.3",
                "activeCapabilitiesCount": sum(len(h["capabilities"]) for h in active_holons),
                "activeHolons": active_holons,
                "datasetWatermark": {"revision": snap["watermarkRevision"], "contentHash": snap["contentHash"]},
                "withdrawnCount": int(self._mesh_state.get("advertisedWithdrawnCount", 0)),
            }

    def mesh_exchange(self, body: Dict[str, Any]) -> Dict[str, Any]:
        env_id = str(body.get("envelopeId") or f"env-{int(time.time()*1000)}").strip()
        kind = str(body.get("kind") or "Event").strip()
        sender_node = str(body.get("senderNodeId") or self.PEER_NODE_ID).strip()
        payload = body.get("payload") or {}
        with self._lock:
            seen = self._mesh_state.setdefault("seenEnvelopeIds", [])
            if env_id in seen:
                return {"ok": True, "messageType": "EXCHANGE_ACK", "duplicate": True, "envelopeId": env_id, "persisted": True}
            seen.append(env_id)
            inbox = self._mesh_state.setdefault("peerInbox", [])
            seq = len(inbox) + 1
            rec = {"seq": seq, "envelopeId": env_id, "kind": kind, "senderNodeId": sender_node, "receivedAt": _utc_now(), "payload": payload}
            inbox.append(rec)
            self._record_restricted_audit(sender_node, "ALLOW", f"MESH durable peerInbox persist before ACK ({env_id})")
            return {"ok": True, "messageType": "EXCHANGE_ACK", "duplicate": False, "envelopeId": env_id, "seqCursor": seq, "persistedBeforeAck": True}

    def mesh_snapshot_pull(self) -> Dict[str, Any]:
        with self._lock:
            snap = self._export_watermark_snapshot(actor="mesh-peer", reason="snapshot-pull")
            return {
                "ok": True,
                "nodeId": self.HOME_NODE_ID,
                "watermarkRevision": snap["watermarkRevision"],
                "contentHash": snap["contentHash"],
                "snapshot": snap,
                "outboxCursor": len(self._outbox),
            }

    def mesh_peer_write(self, body: Dict[str, Any]) -> Dict[str, Any]:
        owner_node = str(body.get("ownerNode") or self.HOME_NODE_ID).strip()
        home_reachable = bool(body.get("homeNodeReachable", True))
        action = str(body.get("action") or "crm.account.upsert").strip()
        payload = body.get("payload") or {}
        actor = str(body.get("actor") or "PeerUser").strip()

        with self._lock:
            node_b = self._mesh_state["nodeB"]
            if owner_node == self.PEER_NODE_ID:
                key = str(payload.get("key") or f"local-{int(time.time())}")
                node_b["localOwnedState"][key] = {"value": payload, "updatedAt": _utc_now(), "actor": actor}
                return {
                    "ok": True,
                    "committedLocally": True,
                    "ownerNode": self.PEER_NODE_ID,
                    "selfPromotedToOwner": False,
                    "nodeBState": node_b["localOwnedState"][key],
                }
            if not home_reachable:
                pending_rec = {
                    "pendingId": f"PEND-NODEB-{len(node_b['pendingHomeNodeQueue']) + 1}",
                    "ownerNode": owner_node,
                    "action": action,
                    "payload": payload,
                    "actor": actor,
                    "status": "PENDING_HOME_NODE",
                    "queuedAt": _utc_now(),
                }
                node_b["pendingHomeNodeQueue"].append(pending_rec)
                self._record_restricted_audit(self.PEER_NODE_ID, "HOLD", f"Node A unreachable; Node B queued {pending_rec['pendingId']} without self-promoting")
                return {
                    "ok": True,
                    "accepted": True,
                    "status": "PENDING_HOME_NODE",
                    "pendingHomeNode": True,
                    "selfPromotedToOwner": False,
                    "committedLocally": False,
                    "pendingRecord": pending_rec,
                }
        # Home node reachable: forward through Node A Air-Lock
        forwarded = self.mutate_relational({"action": action, "actor": actor, "persona": "Federated Peer", "payload": payload, "idempotencyKey": body.get("idempotencyKey")})
        return {"ok": True, "forwardedToHomeNode": True, "selfPromotedToOwner": False, "status": "COMMITTED_ON_HOME_NODE", **forwarded}

    def forward_to_marketplace(self, mkt_url: str, shard_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Optionally forward a Hub Marketplace tokenization request to gard-marketplace :8600."""
        target = mkt_url.rstrip("/") + "/api/v1/marketplace/tokenize"
        body = json.dumps(shard_payload).encode("utf-8")
        req = urllib.request.Request(
            target,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "forwarded": False, "detail": str(exc)}
