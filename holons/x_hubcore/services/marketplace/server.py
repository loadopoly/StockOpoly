"""HTTP REST API Server for Web3 GARD-Shard Shared Marketplace.

Provides high-performance, non-blocking JSON REST API endpoints serving the
HubCore QUIPU Fleet, Loadopoly-OCR, and Bakugo CardCenter.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .engine import GARDMarketplaceEngine

logger = logging.getLogger("gard_marketplace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_ENGINE: Optional[GARDMarketplaceEngine] = None


def get_engine() -> GARDMarketplaceEngine:
    global _ENGINE
    if _ENGINE is None:
        db_path = os.environ.get("MARKETPLACE_DB_PATH") or "Data/hub/marketplace.sqlite"
        data_dir = os.environ.get("MARKETPLACE_DATA_DIR") or "Data/parquet/marketplace"
        _ENGINE = GARDMarketplaceEngine(db_path=Path(db_path), data_dir=Path(data_dir))
    return _ENGINE


class MarketplaceHandler(BaseHTTPRequestHandler):
    server_version = "GARDMarketplace/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), format % args)

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin") or "*"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Device-ID, X-User-ID")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid JSON payload: {exc}") from exc

    # -- the hub-scope boundary -------------------------------------------

    #: Hub scope is tenant data. On the bridge the marketplace cannot see bearer
    #: tokens, so it trusts HubCore's resolved organization only when the request
    #: also carries the shared internal secret. This is defence in depth behind
    #: the air-lock, not a replacement for it: without HubCore in front, nothing
    #: here has authenticated a human.
    ORG_HEADER = "X-HubCore-Org"
    AUTH_HEADER = "X-HubCore-Auth"

    def _internal_org(self) -> str:
        """The organization HubCore resolved, or a refusal."""
        secret = os.environ.get("MARKETPLACE_INTERNAL_SECRET", "")
        presented = self.headers.get(self.AUTH_HEADER) or ""
        org = (self.headers.get(self.ORG_HEADER) or "").strip()
        if not secret:
            raise PermissionError(
                "MARKETPLACE_INTERNAL_SECRET is not configured; hub scope is "
                "refused rather than served unauthenticated"
            )
        if not hmac.compare_digest(presented, secret):
            raise PermissionError(
                "hub scope requires the HubCore internal credential; call through "
                "the air-lock at /api/v1/marketplace/* instead of this service"
            )
        if not org:
            raise PermissionError(
                "hub scope requires a resolved organization header; there is no "
                "cross-organization hub listing"
            )
        return org

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        engine = get_engine()

        try:
            if path in ("/health", "/health/live", "/health/ready"):
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "gard-marketplace",
                        "protocol": "gard-shard/v2",
                        "domain": "SiCi_SQRT(-1)",
                        "time": time.time(),
                    },
                )
            elif path == "/api/v1/marketplace/catalog":
                scope = (query.get("scope", ["shared"])[0] or "shared").strip().lower()
                if scope == "hub":
                    query["org_id"] = [self._internal_org()]
                axis = query.get("axis", [None])[0]
                org_id = query.get("org_id", [None])[0]
                domain = query.get("domain", [None])[0]
                cat = query.get("category", [None])[0]
                lim = int(query.get("limit", [100])[0])
                assets = engine.get_catalog(scope=scope, axis=axis, org_id=org_id, domain=domain, category=cat, limit=lim)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "scope": scope or "shared",
                        "axis_filter": axis or "all",
                        "org_id": org_id,
                        "count": len(assets),
                        "assets": [a.to_dict() for a in assets],
                    },
                )
            elif path == "/api/v1/marketplace/hub/catalog":
                axis = query.get("axis", [None])[0]
                org_id = self._internal_org()
                domain = query.get("domain", [None])[0]
                cat = query.get("category", [None])[0]
                lim = int(query.get("limit", [100])[0])
                assets = engine.get_catalog(scope="hub", axis=axis, org_id=org_id, domain=domain, category=cat, limit=lim)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "scope": "hub",
                        "org_id": org_id,
                        "domain_filter": domain or "all",
                        "count": len(assets),
                        "assets": [a.to_dict() for a in assets],
                    },
                )
            elif path == "/api/v1/marketplace/hub/stats":
                org_id = self._internal_org()
                stats = engine.get_hub_stats(org_id=org_id)
                self._send_json(HTTPStatus.OK, stats)

            elif path.startswith("/api/v1/marketplace/asset/"):
                asset_id = path.split("/api/v1/marketplace/asset/", 1)[1].strip()
                asset = engine.get_asset(asset_id)
                if not asset:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"asset '{asset_id}' not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "asset": asset.to_dict()})

            elif path.startswith("/api/v1/marketplace/portfolio/"):
                user_id = path.split("/api/v1/marketplace/portfolio/", 1)[1].strip()
                portfolio = engine.get_user_portfolio(user_id)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "user_id": user_id,
                        "holdings_count": len(portfolio),
                        "holdings": portfolio,
                    },
                )
            elif path == "/api/v1/marketplace/royalties":
                lim = int(query.get("limit", [50])[0])
                txs = engine.get_recent_transactions(limit=lim)
                self._send_json(HTTPStatus.OK, {"ok": True, "count": len(txs), "transactions": txs})

            elif path == "/api/v1/marketplace/lakehouse":
                self._send_json(HTTPStatus.OK, engine.lakehouse.stats())
            elif path == "/api/v1/marketplace/routing":
                from .engine import SCOPE_ROUTING
                self._send_json(HTTPStatus.OK, {"routing": [
                    {"scope": k, **{kk: list(vv) if isinstance(vv, tuple) else vv
                                    for kk, vv in v.items()}}
                    for k, v in SCOPE_ROUTING.items()]})
            elif path == "/api/v1/marketplace/stats":
                stats = engine.get_stats()
                self._send_json(HTTPStatus.OK, stats)

            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "route not found"})

        except PermissionError as exc:
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("Error handling GET %s", self.path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        engine = get_engine()

        try:
            body = self._read_body_json()

            if path == "/api/v1/marketplace/tokenize":
                required = ["asset_id", "token_id", "axis", "title", "category", "contributor_wallet", "user_id"]
                for req in required:
                    if req not in body:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"missing required field '{req}'"})
                        return

                asset = engine.tokenize_asset(
                    asset_id=str(body["asset_id"]),
                    token_id=str(body["token_id"]),
                    axis=str(body["axis"]),
                    title=str(body["title"]),
                    category=str(body["category"]),
                    contributor_wallet=str(body["contributor_wallet"]),
                    user_id=str(body["user_id"]),
                    shard_count=int(body.get("shard_count", 218)),
                    shard_price_base=float(body.get("shard_price_base", 1.0)),
                    royalty_rate=float(body.get("royalty_rate", 0.05)),
                    is_genesis=bool(body.get("is_genesis", False)),
                    quality_score=float(body.get("quality_score", 0.95)),
                    precision_score=float(body.get("precision_score", 0.95)),
                    metadata=body.get("metadata", {}),
                )
                self._send_json(HTTPStatus.CREATED, {"ok": True, "asset": asset.to_dict()})

            elif path == "/api/v1/marketplace/lakehouse/compact":
                self._send_json(HTTPStatus.OK, engine.lakehouse.compact())
            elif path == "/api/v1/marketplace/lakehouse/migrate":
                self._send_json(HTTPStatus.OK, engine.migrate_history_to_lakehouse())
            elif path == "/api/v1/marketplace/trade":
                for req in ("buyer_id", "seller_id", "asset_id", "shard_count"):
                    if req not in body:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"missing required field '{req}'"})
                        return

                res = engine.execute_trade(
                    buyer_id=str(body["buyer_id"]),
                    seller_id=str(body["seller_id"]),
                    asset_id=str(body["asset_id"]),
                    shard_count=int(body["shard_count"]),
                    unit_price=float(body["unit_price"]) if "unit_price" in body else None,
                    tx_hash=body.get("tx_hash"),
                    block_number=body.get("block_number"),
                )
                self._send_json(HTTPStatus.OK, res)

            elif path == "/api/v1/marketplace/royalties/claim":
                user_id = str(body.get("user_id", "")).strip()
                if not user_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "user_id is required"})
                    return
                claimed = engine.claim_rewards(user_id)
                self._send_json(HTTPStatus.OK, {"ok": True, "user_id": user_id, "claimed_amount": claimed})

            elif path == "/api/v1/marketplace/web3/mint":
                asset_id = str(body.get("asset_id", "")).strip()
                tx_hash = str(body.get("tx_hash", "")).strip()
                if not asset_id or not tx_hash:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "asset_id and tx_hash are required"})
                    return
                contract = body.get("contract_address")
                res = engine.record_on_chain_mint(asset_id, tx_hash, contract)
                self._send_json(HTTPStatus.OK, res)

            elif path == "/api/v1/marketplace/gard/shard":
                asset_id = str(body.get("asset_id", "")).strip()
                payload = body.get("payload", {})
                shards = int(body.get("shard_count", 4))
                secret = body.get("secret")
                context = body.get("model_context")
                envelope = engine.create_gard_envelope(
                    asset_id=asset_id or "asset-unnamed",
                    payload=payload,
                    shard_count=shards,
                    secret=secret,
                    model_context=context,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "envelope": envelope})

            elif path == "/api/v1/marketplace/gard/verify":
                envelope = body.get("envelope")
                if not envelope:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "envelope is required"})
                    return
                secret = body.get("secret")
                res = engine.verify_and_reassemble_envelope(envelope, secret=secret)
                self._send_json(HTTPStatus.OK, {"ok": True, "result": res})

            elif path == "/api/v1/marketplace/hub/tokenize":
                org_id = self._internal_org()
                for req in ("asset_id", "token_id", "title", "domain", "contributor_wallet", "user_id"):
                    if req not in body:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"missing required field '{req}'"})
                        return
                asset = engine.tokenize_supply_chain_asset(
                    asset_id=str(body["asset_id"]),
                    token_id=str(body["token_id"]),
                    title=str(body["title"]),
                    domain=str(body["domain"]),
                    contributor_wallet=str(body["contributor_wallet"]),
                    user_id=str(body["user_id"]),
                    org_id=org_id,
                    hub_id=str(body.get("hub_id", "hub-floor-main")),
                    sku_ref=body.get("sku_ref"),
                    batch_id=body.get("batch_id"),
                    forecast_horizon=body.get("forecast_horizon"),
                    sla_target=body.get("sla_target"),
                    shard_count=int(body.get("shard_count", 500)),
                    shard_price_base=float(body.get("shard_price_base", 10.0)),
                    metadata=body.get("metadata", {}),
                )
                self._send_json(HTTPStatus.CREATED, {"ok": True, "asset": asset.to_dict()})

            elif path == "/api/v1/marketplace/hub/transfer":
                org_id = self._internal_org()
                for req in ("from_user", "to_user", "asset_id", "shard_count"):
                    if req not in body:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"missing required field '{req}'"})
                        return
                res = engine.execute_hub_internal_transfer(
                    from_user=str(body["from_user"]),
                    to_user=str(body["to_user"]),
                    asset_id=str(body["asset_id"]),
                    shard_count=int(body["shard_count"]),
                    reason=str(body.get("reason", "Hub Floor Internal Shard Allocation")),
                    org_id=org_id,
                )
                self._send_json(HTTPStatus.OK, res)

            elif path == "/api/v1/marketplace/hub/sync":
                org_id = self._internal_org()
                hub_id = str(body.get("hub_id", "child-hub-01"))
                worker_id = str(body.get("worker_id", "worker-main"))
                local_allocs = body.get("local_allocations", [])
                res = engine.sync_hub_floor_manifest(
                    hub_id=hub_id,
                    org_id=org_id,
                    worker_id=worker_id,
                    local_allocations=local_allocs,
                )
                self._send_json(HTTPStatus.OK, res)

            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "route not found"})

        except PermissionError as exc:
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("Error handling POST %s", self.path)
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})


def serve(host: str = "0.0.0.0", port: int = 8600) -> None:
    httpd = ThreadingHTTPServer((host, port), MarketplaceHandler)
    logger.info("⚡ Web3 GARD-Shard Shared Marketplace Server running on http://%s:%d", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Marketplace server stopping...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    p = int(os.environ.get("MARKETPLACE_PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8600))
    h = os.environ.get("MARKETPLACE_HOST") or "0.0.0.0"
    serve(host=h, port=p)
