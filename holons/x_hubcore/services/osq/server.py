"""HTTP + Server-Sent Events (SSE) server for x_OSQ Live Network (:8900).

Enables multi-user live channel synchronization across browsers, Loadopoly Portal,
and Holon Hub nodes with Shared Temp and Relationally Static data tiers.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .engine import ConflictError, ForbiddenError, NotFoundError, OsqRelationalAndTempEngine, UnavailableError

logger = logging.getLogger("holon_hub_live")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

ALLOWED_ORIGINS = frozenset({"null", "file://"})
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _cors_origin(origin: str) -> str:
    if not origin or origin in ALLOWED_ORIGINS:
        return "*" if not origin else origin
    parsed = urlparse(origin)
    if parsed.scheme in ("http", "https") and (parsed.hostname or "") in ALLOWED_HOSTS:
        return origin
    return "*"


class HolonHubLiveHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        handler_cls,
        engine: OsqRelationalAndTempEngine,
        html_path: Path,
        crm_html_path: Optional[Path] = None,
        suite_html_path: Optional[Path] = None,
        brief_html_path: Optional[Path] = None,
        openapi_path: Optional[Path] = None,
    ):
        self.engine = engine
        self.html_path = html_path
        self.crm_html_path = crm_html_path or (html_path.parent / "x_CRM.html")
        self.suite_html_path = suite_html_path or (html_path.parent / "suite.html")
        self.brief_html_path = brief_html_path or (html_path.parent / "executive_brief.html")
        self.openapi_path = openapi_path or (html_path.parent.parent / "docs" / "openapi.json")
        super().__init__(server_address, handler_cls)


class OsqHandler(BaseHTTPRequestHandler):
    server_version = "HolonHubLive/2.4"

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), format % args)

    def _cors_headers(self) -> None:
        origin = _cors_origin(self.headers.get("Origin") or "")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Session-Id, Idempotency-Key, Prefer, Last-Event-ID")
        self.send_header("Access-Control-Max-Age", "600")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path: Optional[Path], content_type: str) -> None:
        if not path or not path.exists():
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Asset not found at {path}"})
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path.rstrip("/") or "/"
        qs = parse_qs(parsed_url.query)
        server: HolonHubLiveHttpServer = self.server  # type: ignore[assignment]
        engine = server.engine

        try:
            if path in ("/", "/osq", "/x_OSQ.html", "/Module.html"):
                self._send_static(server.html_path, "text/html; charset=utf-8")
            elif path in ("/crm", "/x_CRM.html", "/x_crm.html"):
                self._send_static(server.crm_html_path, "text/html; charset=utf-8")
            elif path in ("/suite", "/home", "/portal", "/index.html"):
                self._send_static(server.suite_html_path, "text/html; charset=utf-8")
            elif path in ("/brief", "/docs/executive-brief.html"):
                self._send_static(server.brief_html_path, "text/html; charset=utf-8")
            elif path in ("/docs/openapi.json", "/openapi.json"):
                self._send_static(server.openapi_path, "application/json; charset=utf-8")
            elif path == "/api/v1/osq/info":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "product": "HD Supply CPQ & x_OSQ Suite",
                        "version": "2.2.0",
                        "holonVersion": "2.5.0",
                        "liveRuntime": "HolonHubLive",
                        "architecture": "holonhub-live-cyberphysical-io/v2.5",
                        "snowflakeRole": "analytical-export-only",
                        "virtualWarehousesAllowed": False,
                        "homeNodeId": "node-a-osq-home",
                        "host": server.server_address[0],
                        "port": server.server_address[1],
                        "routes": [
                            "/suite",
                            "/crm",
                            "/",
                            "/brief",
                            "/docs/openapi.json",
                            "/health",
                            "/api/v1/osq/state",
                            "/api/v1/osq/mutate",
                            "/api/v1/osq/temp",
                            "/api/v1/osq/stream",
                            "/api/v1/osq/tasks",
                            "/api/v1/osq/reset",
                            "/api/v1/osq/inventory/time-phased",
                            "/api/v1/osq/marketplace/inventory",
                            "/api/v1/osq/logistics/nodal-map",
                            "/api/v1/osq/ifr/auto-po",
                            "/api/v1/osq/ifr/cancel-reconcile",
                            "/api/v1/osq/rfid/event",
                            "/api/v1/osq/quipu/classify-note",
                            "/api/v1/osq/stockopoly/slot-lookup",
                            "/api/v1/osq/sap/signal",
                            "/api/v1/osq/outbox",
                            "/api/v1/osq/pathways",
                            "/api/v1/osq/directives",
                            "/api/v1/osq/directives/feedback",
                            "/api/v1/osq/marketplace/verify",
                            "/api/v1/osq/connectors/execute",
                            "/api/v1/osq/analytics/snapshot",
                            "/api/v1/osq/analytics/query",
                            "/api/v1/hubcore/registrations",
                            "/api/v1/hubcore/register",
                            "/api/v1/hubcore/renew",
                            "/api/v1/hubcore/revoke",
                            "/api/v1/hubcore/audit",
                            "/api/v1/mesh/hello",
                            "/api/v1/mesh/advertise",
                            "/api/v1/mesh/exchange",
                            "/api/v1/mesh/snapshot-pull",
                            "/api/v1/mesh/peer-write",
                        ],
                    },
                )
            elif path in ("/health", "/health/live", "/health/ready"):
                state = engine.get_state()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "product": "HD Supply CPQ & x_OSQ Suite",
                        "version": "2.2.0",
                        "holonVersion": "2.5.0",
                        "liveRuntime": "HolonHubLive",
                        "service": "holonhub-live-engine",
                        "snowflakeRole": "analytical-export-only",
                        "virtualWarehousesAllowed": False,
                        "homeNodeId": "node-a-osq-home",
                        "port": server.server_address[1],
                        "revision": state["revision"],
                        "tempRevision": state["tempRevision"],
                        "outboxCursor": state.get("outboxCursor", 0),
                        "activeSessions": len(state["temp"].get("activeSessions", [])),
                        "time": time.time(),
                    },
                )
            elif path == "/api/v1/osq/state":
                since_raw = (qs.get("sinceCursor") or [None])[0]
                since_val = int(since_raw) if since_raw is not None else None
                self._send_json(HTTPStatus.OK, engine.get_state(since_cursor=since_val))
            elif path == "/api/v1/osq/tasks":
                self._send_json(HTTPStatus.OK, engine.get_tasks())
            elif path.startswith("/api/v1/osq/tasks/"):
                tid = path.split("/api/v1/osq/tasks/", 1)[1]
                self._send_json(HTTPStatus.OK, engine.get_tasks(task_id=tid))
            elif path == "/api/v1/hubcore/registrations":
                self._send_json(HTTPStatus.OK, engine.get_registrations())
            elif path == "/api/v1/hubcore/audit":
                self._send_json(HTTPStatus.OK, engine.get_restricted_audit())
            elif path == "/api/v1/osq/analytics/query":
                dom = str((qs.get("domain") or ["summary"])[0])
                self._send_json(HTTPStatus.OK, engine.query_analytics_readonly(domain=dom))
            elif path in ("/api/v1/mesh/advertise", "/api/v1/mesh/status"):
                self._send_json(HTTPStatus.OK, engine.mesh_advertise())
            elif path == "/api/v1/mesh/snapshot-pull":
                self._send_json(HTTPStatus.OK, engine.mesh_snapshot_pull())
            elif path == "/api/v1/osq/inventory/time-phased":
                dc_q = (qs.get("dc") or [""])[0]
                sku_q = (qs.get("sku") or [""])[0]
                self._send_json(HTTPStatus.OK, engine.get_time_phased_inventory(dc_q, sku_q))
            elif path == "/api/v1/osq/marketplace/inventory":
                tgt_dc = (qs.get("targetDc") or ["TX77"])[0]
                sku_q = (qs.get("sku") or [""])[0]
                try:
                    qty_need = int((qs.get("qty") or ["10"])[0])
                except Exception:
                    qty_need = 10
                inc_virt = str((qs.get("includeVirtual") or ["false"])[0]).lower() == "true"
                try:
                    self._send_json(HTTPStatus.OK, engine.get_unified_marketplace_inventory(tgt_dc, sku_q, qty_need, inc_virt))
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            elif path == "/api/v1/osq/logistics/nodal-map":
                self._send_json(HTTPStatus.OK, engine.get_nodal_logistics_map())
            elif path == "/api/v1/osq/outbox":
                since_raw = (qs.get("sinceCursor") or ["0"])[0]
                limit_raw = (qs.get("limit") or ["200"])[0]
                self._send_json(HTTPStatus.OK, engine.get_outbox(since_cursor=int(since_raw), limit=int(limit_raw)))
            elif path == "/api/v1/osq/pathways":
                self._send_json(HTTPStatus.OK, engine.get_pathways())
            elif path == "/api/v1/osq/directives":
                st_f = (qs.get("status") or [""])[0]
                own_f = (qs.get("ownerRole") or [""])[0]
                self._send_json(HTTPStatus.OK, engine.get_directives(status_filter=st_f, owner_role=own_f))
            elif path == "/api/v1/osq/stream":
                since_raw = (qs.get("sinceCursor") or [self.headers.get("Last-Event-ID")])[0]
                since_val = int(since_raw) if since_raw is not None and str(since_raw).isdigit() else None
                self._handle_sse(engine, since_cursor=since_val)
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "route not found"})
        except NotFoundError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        server: HolonHubLiveHttpServer = self.server  # type: ignore[assignment]
        engine = server.engine

        try:
            body = self._read_json()
            if path == "/api/v1/osq/mutate":
                if not body.get("idempotencyKey") and self.headers.get("Idempotency-Key"):
                    body["idempotencyKey"] = self.headers.get("Idempotency-Key")
                if not body.get("sessionId") and self.headers.get("X-Session-Id"):
                    body["sessionId"] = self.headers.get("X-Session-Id")
                prefer_async = "respond-async" in str(self.headers.get("Prefer") or "").lower()
                is_async = bool(body.get("async") or prefer_async)
                state = engine.mutate_relational(body)
                if is_async:
                    state["accepted"] = True
                    self._send_json(HTTPStatus.ACCEPTED, state)
                else:
                    self._send_json(HTTPStatus.OK, state)
            elif path == "/api/v1/osq/temp":
                state = engine.update_temp(body)
                status = HTTPStatus.CONFLICT if state.get("conflict") else HTTPStatus.OK
                self._send_json(status, state)
            elif path == "/api/v1/osq/reset":
                state = engine.reset_to_seed()
                self._send_json(HTTPStatus.OK, state)
            elif path == "/api/v1/osq/marketplace/verify":
                self._send_json(HTTPStatus.OK, engine.verify_marketplace_artifact(body))
            elif path == "/api/v1/osq/marketplace/sync":
                if body.get("trustEnvelope"):
                    res = engine.verify_marketplace_artifact(body)
                    self._send_json(HTTPStatus.OK, res)
                    return
                mkt_url = str(body.get("marketplaceUrl") or "http://127.0.0.1:8600")
                shard_res = engine.mutate_relational(
                    {
                        "action": "marketplace.tokenize",
                        "actor": body.get("actor", "osqEngine"),
                        "persona": body.get("persona", "RP Ops Planner"),
                        "payload": body.get("payload") or {},
                    }
                )
                fwd = engine.forward_to_marketplace(mkt_url, body.get("payload") or {})
                self._send_json(HTTPStatus.OK, {"ok": True, "marketplaceForward": fwd, **shard_res})
            elif path == "/api/v1/osq/connectors/execute":
                res = engine.execute_connector(body)
                status = HTTPStatus.ACCEPTED if res.get("status") == "HOLD" else HTTPStatus.OK
                self._send_json(status, res)
            elif path == "/api/v1/osq/analytics/snapshot":
                self._send_json(HTTPStatus.OK, engine.export_analytics_snapshot(actor=str(body.get("actor") or "system")))
            elif path == "/api/v1/hubcore/register":
                self._send_json(HTTPStatus.OK, engine.register_holon(body))
            elif path == "/api/v1/hubcore/renew":
                self._send_json(HTTPStatus.OK, engine.renew_registration(body))
            elif path == "/api/v1/hubcore/revoke":
                self._send_json(HTTPStatus.OK, engine.revoke_registration(body))
            elif path == "/api/v1/mesh/hello":
                self._send_json(HTTPStatus.OK, engine.mesh_hello(body))
            elif path == "/api/v1/mesh/advertise":
                self._send_json(HTTPStatus.OK, engine.mesh_advertise())
            elif path == "/api/v1/mesh/exchange":
                self._send_json(HTTPStatus.OK, engine.mesh_exchange(body))
            elif path == "/api/v1/mesh/peer-write":
                res = engine.mesh_peer_write(body)
                status = HTTPStatus.ACCEPTED if res.get("pendingHomeNode") else HTTPStatus.OK
                self._send_json(status, res)
            elif path in (
                "/api/v1/osq/ifr/auto-po",
                "/api/v1/osq/ifr/cancel-reconcile",
                "/api/v1/osq/rfid/event",
                "/api/v1/osq/quipu/classify-note",
                "/api/v1/osq/stockopoly/slot-lookup",
                "/api/v1/osq/sap/signal",
                "/api/v1/osq/directives/feedback",
            ):
                action_map = {
                    "/api/v1/osq/ifr/auto-po": "ifr.auto_po",
                    "/api/v1/osq/ifr/cancel-reconcile": "order.cancel_reconcile",
                    "/api/v1/osq/rfid/event": "rfid.event",
                    "/api/v1/osq/quipu/classify-note": "quipu.classify_note",
                    "/api/v1/osq/stockopoly/slot-lookup": "stockopoly.slot_lookup",
                    "/api/v1/osq/sap/signal": "sap.signal",
                    "/api/v1/osq/directives/feedback": "directive.feedback",
                }
                wrap = {
                    "action": action_map[path],
                    "actor": body.get("actor") or "HolonHubLive-API",
                    "persona": body.get("persona") or "Ops / Planner",
                    "payload": body.get("payload") if isinstance(body.get("payload"), dict) else body,
                }
                if body.get("idempotencyKey"):
                    wrap["idempotencyKey"] = body["idempotencyKey"]
                self._send_json(HTTPStatus.OK, engine.mutate_relational(wrap))
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "route not found"})
        except ConflictError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"ok": False, "conflict": True, "error": str(exc)})
        except UnavailableError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
        except ForbiddenError as exc:
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
        except NotFoundError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("POST error on %s", path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def _handle_sse(self, engine: OsqRelationalAndTempEngine, since_cursor: Optional[int] = None) -> None:
        q_events: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=32)

        def push(packet: Dict[str, Any]) -> None:
            try:
                q_events.put_nowait(packet)
            except queue.Full:
                try:
                    q_events.get_nowait()
                    q_events.put_nowait(packet)
                except Exception:
                    pass

        engine.subscribe(push)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors_headers()
        self.end_headers()

        try:
            init_state = engine.get_state(since_cursor=since_cursor)
            cur = init_state.get("outboxCursor", 0)
            init_data = json.dumps({"type": "connected", "cursor": cur, "state": init_state}, ensure_ascii=False)
            self.wfile.write(f"id: {cur}\nevent: sync\ndata: {init_data}\n\n".encode("utf-8"))
            self.wfile.flush()

            while True:
                try:
                    packet = q_events.get(timeout=15.0)
                    p_cur = packet.get("cursor", "")
                    msg = json.dumps(packet, ensure_ascii=False)
                    self.wfile.write(f"id: {p_cur}\nevent: sync\ndata: {msg}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            engine.unsubscribe(push)


def serve(
    host: str = "127.0.0.1",
    port: int = 8900,
    html_path: Optional[Path] = None,
    crm_html_path: Optional[Path] = None,
    suite_html_path: Optional[Path] = None,
    brief_html_path: Optional[Path] = None,
    openapi_path: Optional[Path] = None,
) -> None:
    product_env = os.environ.get("OSQ_PRODUCT_ROOT")
    product_root = Path(product_env).resolve() if product_env else next(
        (p for p in Path(__file__).resolve().parents if (p / "app" / "x_OSQ.html").exists()),
        None,
    )
    if product_root is not None:
        resolved_html = Path(html_path or (product_root / "app" / "x_OSQ.html")).resolve()
        resolved_crm_html = Path(crm_html_path or (product_root / "app" / "x_CRM.html")).resolve()
        resolved_suite_html = Path(suite_html_path or (product_root / "app" / "suite.html")).resolve()
        resolved_brief_html = Path(brief_html_path or (product_root / "app" / "executive_brief.html")).resolve()
        resolved_openapi = Path(openapi_path or (product_root / "docs" / "openapi.json")).resolve()
        engine = OsqRelationalAndTempEngine(repo_root=product_root)
    else:
        repo_root = Path(__file__).resolve().parents[2]
        resolved_html = Path(html_path or (repo_root / "Loadopoly-Portal" / "x_OSQ.html")).resolve()
        resolved_crm_html = Path(crm_html_path or (repo_root / "Loadopoly-Portal" / "x_CRM.html")).resolve()
        resolved_suite_html = Path(suite_html_path or (repo_root / "Loadopoly-Portal" / "index.html")).resolve()
        resolved_brief_html = Path(brief_html_path or (repo_root / "Loadopoly-Portal" / "executive_brief.html")).resolve()
        resolved_openapi = Path(openapi_path or (repo_root / "docs" / "openapi.json")).resolve()
        engine = OsqRelationalAndTempEngine(repo_root=repo_root)

    server = HolonHubLiveHttpServer(
        (host, port),
        OsqHandler,
        engine=engine,
        html_path=resolved_html,
        crm_html_path=resolved_crm_html,
        suite_html_path=resolved_suite_html,
        brief_html_path=resolved_brief_html,
        openapi_path=resolved_openapi,
    )
    logger.info("HD Supply CPQ & x_OSQ HolonHubLive Suite listening on http://%s:%d/suite (OSQ: %s, CRM: %s)", host, port, resolved_html, resolved_crm_html)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping HolonHubLive server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    p = int(os.environ.get("OSQ_PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8900))
    h = os.environ.get("OSQ_HOST") or "127.0.0.1"
    serve(host=h, port=p)
