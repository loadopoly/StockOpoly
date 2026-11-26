"""The HubCore native token broker and Data-Only Ingress Air-Lock (:8000).

This is the only process an external client ever reaches. It is deliberately
built on the standard library so it has no web-framework supply chain, and it
binds to loopback by default (V10-END-001).

Routes:

    GET  /                    the generated Hub single-page application
    GET  /health              liveness, no authentication, no internal detail
    GET  /health/ready        readiness: required fleet endpoints are healthy
    GET  /.well-known/jwks    the RS256 verification key
    POST /api/v1/session      mint a scoped tenant token for a provisioned space
    GET  /api/v1/endpoints    the endpoint manifest this token may see
    POST /api/v1/ingress      data-only submission into the training intake queue
    GET  /api/v1/fleet        fleet status (Reviewer or Admin role only)
    GET  /api/v1/status       bounded operational snapshot for the Hub UI

The Hub UI is served from this same origin so the browser experience is a real
client of the authority rather than a static page guessing at its state.

What this service does NOT expose, by construction: any filesystem path,
database location, provider secret, release credential, shell, or arbitrary
query. Every response is bounded, and every refusal is a policy error rather
than a plausible empty result (Part 7.2).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import (
    advisor,
    airlock,
    allocations,
    authz,
    annealing,
    bus,
    config,
    corpus,
    dataplane,
    endpoints,
    expert,
    federation,
    hal,
    marketplace_client,
    modules,
    releaser,
    spatial,
    tasks,
    tunnels,
    vadmin,
)
from .errors import HubCoreError, PolicyError, TokenError
from .token_broker import TokenBroker, endpoint_manifest

logger = logging.getLogger("hubcore.airlock")

MAX_REQUEST_BYTES = 1024 * 1024
INTAKE_QUEUE_NAME = "quipu_training_intake.jsonl"

#: Origins the browser experience may legitimately carry. The Hub is served
#: from this origin, an Electron shell reports ``file://`` as ``null``, and a
#: developer may open the staged file directly. Nothing else is echoed back:
#: a wildcard here would let any visited page drive the authority (Part 1.3).
ALLOWED_ORIGINS = frozenset({"null", "file://"})
ALLOWED_ORIGIN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: The only two marketplace scopes. HubCore enforces this before forwarding,
#: so a typo never reaches the store and never widens a query.
VALID_MARKETPLACE_SCOPES = frozenset({"shared", "hub"})

#: How long a fleet probe result may be reused. Without this, every dashboard
#: paint re-probes every model endpoint.
FLEET_CACHE_SECONDS = 10.0


def origin_is_allowed(origin: str) -> bool:
    """True when ``origin`` is a loopback or local-file browser origin."""
    if not origin:
        return False
    if origin in ALLOWED_ORIGINS:
        return True
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "") in ALLOWED_ORIGIN_HOSTS


def _forward_to_quipu_async(record: Dict[str, Any]) -> None:
    def _worker() -> None:
        try:
            quipu_url = os.environ.get("QUIPU_URL") or "http://127.0.0.1:7100"
            url = f"{quipu_url.rstrip('/')}/observe"
            payload = {
                "source": "hubcore",
                "kind": "governance",
                "text": record.get("text") or json.dumps(record.get("payload", {})),
                "meta": {
                    "idempotencyKey": record.get("idempotencyKey"),
                    "schemaVersion": record.get("schemaVersion"),
                },
            }
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=3.0):
                pass
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True, name="hubcore-quipu-forward").start()


class TrainingIntakeQueue:
    """Append-only, deduplicating queue in front of the QUIPU annealing loop.

    Duplicate submissions cannot create duplicate training signals: the
    idempotency key is the payload content hash (Part 4.1).
    Verified records are queued locally and forwarded asynchronously to the
    active QUIPU Docker container.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seen = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    self._seen.add(json.loads(line)["idempotencyKey"])
                except Exception:
                    continue

    def enqueue(self, record: Dict[str, Any]) -> Tuple[bool, str]:
        key = record["idempotencyKey"]
        with self._lock:
            if key in self._seen:
                return False, "duplicate"
            self._seen.add(key)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        _forward_to_quipu_async(record)
        return True, "queued"

    def depth(self) -> int:
        return len(self._seen)


class HubCoreState:
    """Process-wide state shared by every request handler."""

    def __init__(self, hub_dir: Optional[Path] = None):
        documents = config.load_all(hub_dir)
        self.org = documents["org"]
        self.workspace = documents["workspace"]
        self.modules = documents["modules"]
        self.library_root = config.library_root(self.workspace)
        self.allocations = allocations.AllocationEngine(self.library_root, self.workspace)
        self.broker = TokenBroker()
        self.broker.ensure_keypair()

        self.module_registry = modules.ModuleRegistry(self.modules)
        store = self.workspace.get("transactionalStore", {})
        self.task_manager = tasks.TaskManager(
            self.library_root / store.get("path", "pipeline/local_brain.sqlite")
        )
        self.release_engine = releaser.ReleaseEngine(self.workspace)
        self.expert_ledger = expert.ExpertLedger(self.workspace)

        # v-ADMIN is the disclosed synthetic assistant on the Admin-visibility
        # path (Part 10.4). The advisor holds a reference to it so grades 4 and
        # 5 produce their parallel Admin record, and so every human-only gate
        # can refuse a synthetic actor by asking one authority.
        self.vadmin = vadmin.VirtualAdmin(
            persona=self.org.get("virtualPersona") or "Assistant",
            ask_persona=self.org.get("askPersona") or "Ask Advisor",
            expert_ledger=self.expert_ledger,
            enabled=bool(self.org.get("capabilities", {}).get("virtualPersona", True)),
        )
        self.corpus = corpus.build(documents=documents)
        self.advisor_pipeline = advisor.AdvisorPipeline(
            persona=self.org.get("askPersona") or "Ask Advisor",
            corpus=self.corpus,
            db_path=self.library_root / store.get("path", "pipeline/local_brain.sqlite"),
            expert_ledger=self.expert_ledger,
            vadmin=self.vadmin,
        )
        self.federation = federation.FederationCoordinator(self.workspace, self.org)
        self.child_hubs = tunnels.ChildHubRegistry(self.workspace)
        self.rdp_bridge = tunnels.BoundedRdpBridge(self.workspace, self.child_hubs)

        federation_cfg = self.org.get("federation", {})
        gate = airlock.ContaminationGate(
            forbidden_strings=airlock.forbidden_strings_from_env()
        )
        lock = self.workspace.get("airlock", {})
        self.airlock = airlock.AirLock(
            gate=gate,
            max_payload_bytes=int(lock.get("maxPayloadBytes", MAX_REQUEST_BYTES)),
            tier=int(federation_cfg.get("consentTier", 0)),
        )
        self.queue = TrainingIntakeQueue(
            self.library_root / "Data" / "hub" / "inbox" / INTAKE_QUEUE_NAME
        )

        # The seam between the air-lock and the annealing loop. Without it the
        # intake queue is a dead end: screened external observations accumulate
        # in a file and QUIPU's `hubcore` perception source never receives one.
        self.axes = annealing.AxisRegistry(self.workspace)
        # Touch -> Space: measured metrology grounding warehouse geometry.
        self.spatial = spatial.bridge_for(self.workspace, self.expert_ledger)
        self.forwarder = annealing.IntakeForwarder(
            queue_path=self.queue.path,
            observer_url=annealing.observer_url(self.workspace),
            db_path=self.library_root / store.get("path", "pipeline/local_brain.sqlite"),
        )
        self.forwarder_loop = annealing.ForwarderLoop(self.forwarder)
        self.message_bus = bus.DurableMessageBus(self.workspace)
        self.data_plane = dataplane.DataPlaneEngine(self.workspace)

        # HubCore is the centralization and policy layer, not the owner of
        # marketplace state. gard-marketplace owns the Web3 store and is the
        # single writer of both its SQLite ledger and its Parquet partitions;
        # HubCore authenticates the caller, resolves the organization from the
        # token, and forwards. Embedding the engine here as well gave the same
        # files two writers, which Parquet cannot survive.
        self.marketplace = None
        self.marketplace_error = ""
        try:
            registry = endpoints.load_registry(self.workspace)
            descriptor = registry.get("gardMarketplace")
            if descriptor is None:
                raise HubCoreError(
                    "workspace declares no 'gardMarketplace' endpoint; the Web3 "
                    "store has no home"
                )
            self.marketplace = marketplace_client.RemoteMarketplace(descriptor.base_url)
        except Exception as exc:
            self.marketplace_error = type(exc).__name__ + ": " + str(exc)
            logger.error("marketplace client unavailable: %s", self.marketplace_error)

        library = self.workspace.get("library", {})
        self.app_file = self.library_root / library.get("prod", "PROD") / "Hub_v10.html"
        self.started_at = time.time()
        self.hardware = hal.probe(self.workspace).to_dict()
        self._fleet_cache: Optional[Dict[str, Any]] = None
        self._fleet_cache_at = 0.0
        self._fleet_lock = threading.Lock()

    def annealing_status(self) -> Dict[str, Any]:
        """Loop throughput, plus why the queue is the depth it is.

        An idle loop with nothing pending reads as healthy, and at consent
        tier 0 that is exactly wrong: the membrane is closed, so no external
        observation can arrive at all. The reason travels with the number.
        """
        status = self.forwarder.status()
        federation = self.org.get("federation", {})
        tier = int(federation.get("consentTier", 0))
        accepted = sorted(airlock.TIER_ARTIFACTS.get(tier) or ())
        status["ingress"] = {
            "consentTier": tier,
            "federationEnabled": bool(federation.get("enabled")),
            "acceptsArtifactKinds": accepted,
            "open": bool(accepted),
            "reason": (
                "consent tier " + str(tier) + " accepts no artifact kind, so the "
                "membrane refuses every external submission; an empty queue here "
                "means the door is shut, not that clients are idle"
                if not accepted else
                "consent tier " + str(tier) + " accepts: " + ", ".join(accepted)
            ),
        }
        return status

    def fleet_status(self, max_age: float = FLEET_CACHE_SECONDS) -> Dict[str, Any]:
        """Fleet posture, re-probed at most once per ``max_age`` seconds."""
        with self._fleet_lock:
            fresh = self._fleet_cache is not None and (time.time() - self._fleet_cache_at) < max_age
            if not fresh:
                self._fleet_cache = endpoints.status_report(self.workspace, timeout=2.0)
                self._fleet_cache_at = time.time()
            return self._fleet_cache


class Handler(BaseHTTPRequestHandler):
    """Routing, authentication, and bounded I/O for the air-lock."""

    server_version = "HubCore/10.0"
    sys_version = ""
    state: HubCoreState

    #: Verified claims for the current request, set by the dispatcher. ``None``
    #: only on routes the policy table marks public.
    claims: Any = None

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), format % args)

    def _cors_headers(self) -> None:
        """Echo an allowed browser origin, or none at all.

        The Hub is a browser application, so without this every call it makes
        is discarded by the user agent before HubCore ever sees a response --
        the page then looks alive while knowing nothing.
        """
        origin = self.headers.get("Origin") or ""
        if not origin_is_allowed(origin):
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Credentials", "false")

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        """Answer the CORS preflight the Hub sends before every POST."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _refuse(self, status: int, exc: Exception) -> None:
        code = getattr(exc, "code", "hubcore.error")
        self._send(status, {"error": code, "detail": str(exc)})

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise PolicyError("Content-Length is not an integer")
        if length <= 0:
            raise PolicyError("request body is required")
        if length > MAX_REQUEST_BYTES:
            raise PolicyError(
                "request body of " + str(length) + " bytes exceeds the membrane limit"
            )
        raw = self.rfile.read(length)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolicyError("request body is not valid JSON: " + str(exc)) from exc
        if not isinstance(document, dict):
            raise PolicyError("request body must be a JSON object")
        return document

    def _actor(self) -> str:
        """Who is doing this, according to the verified token.

        Never the request body. A route that records an actor from caller input
        records an assertion, not an identity, and the audit trail is then a list
        of things people claimed about themselves.
        """
        if self.claims is None:
            raise TokenError("this route records an actor and requires a token")
        return self.claims.subject

    def _query(self) -> Dict[str, str]:
        parsed = urlparse(self.path)
        return {k: v[0] for k, v in parse_qs(parsed.query).items() if v}

    def _hub_scope_claims(self, requested_org: str):
        """Authorize a read or write against one organization's hub marketplace.

        The org hub marketplace carries internal supply-chain records --
        forecasting models, inventory lots, procurement SLAs, cost centres.
        Serving them from a query parameter with no token would make the
        air-lock an anonymous read of tenant data (Part 12.5).
        """
        claims = self._claims()
        org = (requested_org or "").strip() or claims.tenancy.org_id
        if org != claims.tenancy.org_id:
            raise PolicyError(
                "token is scoped to organization " + claims.tenancy.org_id
                + " and cannot read or write the hub marketplace of " + org
            )
        return claims, org

    def _claims(self):
        header = self.headers.get("Authorization") or ""
        if not header.lower().startswith("bearer "):
            raise TokenError("a bearer token is required")
        return self.state.broker.verify(header[7:].strip())

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        routes: Dict[str, Callable[[], None]] = {
            "/": self._app,
            "/index.html": self._app,
            "/health": self._health,
            "/health/live": self._health,
            "/health/ready": self._ready,
            "/.well-known/jwks": self._jwks,
            "/api/v1/endpoints": self._endpoints,
            "/api/v1/fleet": self._fleet,
            "/api/v1/status": self._status,
            "/api/v1/chains": self._chains,
            "/api/v1/tasks/audit": self._tasks_audit,
            "/api/v1/modules": self._modules_list,
            "/api/v1/tasks": self._tasks_list,
            "/api/v1/expert/ledger": self._expert_ledger,
            "/api/v1/advisor/requests": self._advisor_requests,
            "/api/v1/advisor/request": self._advisor_request_get,
            "/api/v1/advisor/grades": self._advisor_grades,
            "/api/v1/advisor/quality": self._advisor_quality,
            "/api/v1/vadmin": self._vadmin_status,
            "/api/v1/vadmin/queue": self._vadmin_queue,
            "/api/v1/federation/mesh": self._federation_mesh,
            "/api/v1/annealing": self._annealing_status,
            "/api/v1/mesh/axes": self._mesh_axes,
            "/api/v1/spatial": self._spatial_status,
            "/api/v1/spatial/ground-truth": self._spatial_preview,
            "/api/v1/hubs/children": self._child_hubs_list,
            "/api/v1/hubs/children/tasks": self._child_hubs_tasks,
            "/api/v1/marketplace/catalog": self._marketplace_catalog,
            "/api/v1/marketplace/routing": self._marketplace_routing,
            "/api/v1/marketplace/lakehouse": self._marketplace_lakehouse,
            "/api/v1/marketplace/hub/catalog": self._marketplace_hub_catalog,
            "/api/v1/marketplace/hub/stats": self._marketplace_hub_stats,
            "/api/v1/marketplace/stats": self._marketplace_stats,
            "/api/v1/marketplace/royalties": self._marketplace_royalties,
            "/api/v1/bus/status": self._bus_status,
            "/api/v1/directives": self._directives_list,
        }
        self._dispatch(routes)

    def do_POST(self) -> None:  # noqa: N802
        routes: Dict[str, Callable[[], None]] = {
            "/api/v1/session": self._session,
            "/api/v1/ingress": self._ingress,
            "/api/v1/tasks": self._tasks_create,
            "/api/v1/tasks/hold": self._tasks_hold,
            "/api/v1/tasks/accept": self._tasks_accept,
            "/api/v1/tasks/reject": self._tasks_reject,
            "/api/v1/release/promote": self._release_promote,
            "/api/v1/advisor/query": self._advisor_query,
            "/api/v1/advisor/request": self._advisor_submit,
            "/api/v1/advisor/grade-override": self._advisor_override,
            "/api/v1/advisor/review": self._advisor_review,
            "/api/v1/advisor/admin-review": self._advisor_admin_review,
            "/api/v1/advisor/send": self._advisor_send,
            "/api/v1/advisor/followup": self._advisor_followup,
            "/api/v1/advisor/close": self._advisor_close,
            "/api/v1/support/ticket": self._support_ticket,
            "/api/v1/vadmin/kill-switch": self._vadmin_kill_switch,
            "/api/v1/vadmin/queue/resolve": self._vadmin_resolve,
            "/api/v1/vadmin/learning-proposal": self._vadmin_proposal,
            "/api/v1/federation/gradient": self._federation_gradient,
            "/api/v1/annealing/drain": self._annealing_drain,
            "/api/v1/spatial/ground": self._spatial_ground,
            "/api/v1/spatial/stockopoly-slot": self._spatial_stockopoly_slot,
            "/api/v1/bus/relay": self._bus_relay,
            "/api/v1/directives/feedback": self._directives_feedback,
            "/api/v1/dataplane/watermark": self._dataplane_watermark,
            "/api/v1/hubs/children/register": self._child_hubs_register,
            "/api/v1/hubs/children/proposals": self._child_hubs_proposals,
            "/api/v1/hubs/children/session": self._child_hubs_session,
            "/api/v1/marketplace/tokenize": self._marketplace_tokenize,
            "/api/v1/marketplace/hub/tokenize": self._marketplace_hub_tokenize,
            "/api/v1/marketplace/hub/transfer": self._marketplace_hub_transfer,
            "/api/v1/marketplace/hub/sync": self._marketplace_hub_sync,
            "/api/v1/marketplace/trade": self._marketplace_trade,
            "/api/v1/marketplace/gard/shard": self._marketplace_gard_shard,
            "/api/v1/marketplace/gard/verify": self._marketplace_gard_verify,
            "/api/v1/marketplace/web3/mint": self._marketplace_web3_mint,
        }
        self._dispatch(routes)

    @classmethod
    def declared_routes(cls):
        """Every (method, path) this handler serves, for the startup check."""
        probe = cls.__new__(cls)
        found = []
        for method, attr in (("GET", "do_GET"), ("POST", "do_POST")):
            captured = {}

            def capture(routes, _c=captured):
                _c.update(routes)

            probe._dispatch = capture  # type: ignore[method-assign]
            getattr(probe, attr)()
            found.extend((method, path) for path in captured)
        return found

    def _dispatch(self, routes: Dict[str, Callable[[], None]]) -> None:
        path = self.path.split("?", 1)[0]
        path = path.rstrip("/") or "/"
        handler = routes.get(path) or routes.get(path + "/")
        if handler is None:
            self._send(HTTPStatus.NOT_FOUND, {"error": "hubcore.route.unknown", "detail": "no such route"})
            return
        try:
            # Authorization happens here, once, for every route. Expressed as a
            # `self._claims()` call inside each handler it was forgotten on 59 of
            # 67 routes, and nothing failed when it was (V10-SEC-001).
            self.claims = None
            required = authz.policy_for(self.command, path)
            if required is None:
                raise PolicyError(
                    "route " + path + " has no authorization policy and is "
                    "refused rather than served at an undefined access level"
                )
            if required != authz.PUBLIC:
                self.claims = self._claims()
                authz.assert_role(self.claims, required, self.command, path)
            handler()
        except marketplace_client.MarketplaceUnavailable as exc:
            # An unreachable upstream is not a bad request. It is this service
            # being unable to answer, and it says so with the right status.
            self._refuse(HTTPStatus.SERVICE_UNAVAILABLE, exc)
        except TokenError as exc:
            self._refuse(HTTPStatus.UNAUTHORIZED, exc)
        except PolicyError as exc:
            self._refuse(HTTPStatus.FORBIDDEN, exc)
        except HubCoreError as exc:
            self._refuse(HTTPStatus.BAD_REQUEST, exc)
        except Exception as exc:  # never leak a traceback across the membrane
            logger.exception("unhandled error on %s", self.path)
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "hubcore.internal", "detail": type(exc).__name__},
            )

    def _app(self) -> None:
        """Serve the generated Hub SPA from the authority's own origin."""
        path = self.state.app_file
        if not path.exists():
            self._send(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "hubcore.app.missing",
                    "detail": "Hub_v10.html has not been generated; run "
                    "'python -m services.hubcore.cli generate-hub'",
                },
            )
            return
        self._send_html(HTTPStatus.OK, path.read_bytes())

    def _health(self) -> None:
        self._send(HTTPStatus.OK, {"status": "ok", "service": "hubcore-token-broker"})

    def _ready(self) -> None:
        report = self.state.fleet_status()
        unhealthy = report["requiredUnhealthy"]
        status = HTTPStatus.OK if not unhealthy else HTTPStatus.SERVICE_UNAVAILABLE
        self._send(
            status,
            {
                "status": "ready" if not unhealthy else "degraded",
                "unhealthy": unhealthy,
                "checkedAt": report["checkedAt"],
                "compliant": report["compliant"],
                "bindingFindings": report["bindingFindings"],
                "endpoints": report["endpoints"],
            },
        )

    def _status(self) -> None:
        """One bounded snapshot the Hub dashboard renders directly.

        Every number the UI shows comes from here, so the dashboard cannot
        drift into displaying values that were true only at generation time.
        """
        state = self.state
        report = state.fleet_status()
        org = state.org
        federation_status = state.federation.status()
        task_counts = state.task_manager.counts_by_state()
        advisor_metrics = state.advisor_pipeline.quality_metrics()
        self._send(
            HTTPStatus.OK,
            {
                "hubcoreVersion": "10.0.0",
                "identity": {
                    "orgId": org["orgId"],
                    "orgName": org.get("orgName"),
                    "hubName": org.get("hubName"),
                    "askPersona": org.get("askPersona"),
                    "virtualPersona": org.get("virtualPersona"),
                },
                "uptimeSeconds": round(time.time() - state.started_at, 1),
                "hardware": {
                    "tier": state.hardware.get("tier"),
                    "tierName": state.hardware.get("tier_name"),
                    "gpus": state.hardware.get("gpus", []),
                    "totalVramMb": state.hardware.get("total_vram_mb"),
                    "heavyModelBudgetMb": state.hardware.get("heavy_model_budget_mb"),
                    "concurrentResidentModels": state.hardware.get("concurrent_resident_models"),
                },
                "fleet": {
                    "checkedAt": report["checkedAt"],
                    "compliant": report["compliant"],
                    "requiredUnhealthy": report["requiredUnhealthy"],
                    "bindingFindings": report["bindingFindings"],
                    "healthy": sum(1 for e in report["endpoints"] if e["healthy"]),
                    "total": len(report["endpoints"]),
                    "requiredHealthy": sum(
                        1 for e in report["endpoints"] if e["required"] and e["healthy"]
                    ),
                    "requiredTotal": sum(1 for e in report["endpoints"] if e["required"]),
                    "endpoints": report["endpoints"],
                },
                "chains": org.get("chains", []),
                "modules": {
                    "total": len(state.module_registry.list_all()),
                    "byChain": state.module_registry.by_chain(),
                },
                "tasks": {
                    "byState": task_counts,
                    "total": sum(task_counts.values()),
                },
                "airlock": {
                    "mode": state.workspace.get("airlock", {}).get("mode", "data-only"),
                    "zeroExternalCompute": bool(
                        state.workspace.get("airlock", {}).get("zeroExternalCompute", True)
                    ),
                    "queueDepth": state.queue.depth(),
                },
                "federation": federation_status,
                "childHubs": len(state.child_hubs.list_all()),
                "advisor": {
                    "persona": org.get("askPersona"),
                    "corpusPassages": len(state.corpus),
                    "byState": advisor_metrics["byState"],
                    "byGrade": advisor_metrics["byGrade"],
                    "total": advisor_metrics["totalRequests"],
                    "overrideRate": advisor_metrics["overrideRate"],
                },
                "vadmin": state.vadmin.status(),
                "annealing": state.annealing_status(),
                "messageBus": state.message_bus.status(),
                "mesh": {
                    key: value
                    for key, value in state.axes.summary(
                        {e["id"]: e["healthy"] for e in report["endpoints"]}
                    ).items()
                    if key != "axes"
                },
            },
        )

    def _chains(self) -> None:
        """Seven Chains with the modules actually registered against each."""
        by_chain = self.state.module_registry.by_chain()
        chains = [
            {**chain, "modules": by_chain.get(chain.get("label", ""), [])}
            for chain in self.state.org.get("chains", [])
        ]
        self._send(
            HTTPStatus.OK,
            {"chains": chains, "unassigned": by_chain.get("General", [])},
        )

    def _jwks(self) -> None:
        self._send(HTTPStatus.OK, {"keys": [self.state.broker.public_jwk()]})

    def _session(self) -> None:
        """Mint a scoped token, provisioning the allocation space first.

        V10-ALC-002: no endpoint or model may be invoked before the space,
        quota, and schema isolation exist.
        """
        body = self._read_json()
        workspace = self.state.workspace
        tenancy = allocations.TenancyTuple(
            org_id=allocations.validate_id(body.get("orgId", ""), "orgId"),
            site_id=allocations.validate_id(
                body.get("siteId") or workspace.get("siteId", "site-default"), "siteId"
            ),
            device_id=allocations.validate_id(
                body.get("deviceId") or workspace.get("deviceId", "dev-default"), "deviceId"
            ),
            repo_id=allocations.slugify_repo(str(body.get("repoId", ""))),
            user_id=allocations.validate_id(body.get("userId", ""), "userId"),
        )
        if tenancy.org_id != self.state.org["orgId"] and not _org_is_registered(
            self.state, tenancy.org_id
        ):
            raise PolicyError(
                "organization " + tenancy.org_id + " is not registered on this node; "
                "onboarding requires an Admin decision"
            )

        record = self.state.allocations.provision(tenancy)
        self.state.allocations.assert_within_quota(tenancy, record["quotaMb"])

        # Requiring a token everywhere achieves nothing if a caller can mint
        # itself an Admin one. Reviewer and Admin need the operator credential
        # this node was started with.
        requested_role = str(body.get("role") or "Requester")
        authz.assert_may_mint(
            requested_role,
            self.headers.get(authz.OPERATOR_HEADER) or "",
            os.environ.get(authz.OPERATOR_SECRET_ENV, ""),
        )

        token = self.state.broker.mint(
            tenancy,
            subject=str(body.get("subject") or tenancy.user_id),
            role=requested_role,
            capabilities=body.get("capabilities") or ["data.read"],
            ttl_seconds=int(body.get("ttlSeconds") or 900),
        )
        claims = self.state.broker.verify(token)
        self._send(
            HTTPStatus.CREATED,
            {
                "token": token,
                "tokenType": "Bearer",
                "expiresAt": claims.expires_at,
                "allocationSpaceId": claims.allocation_space_id,
                "manifest": endpoint_manifest(workspace, claims),
            },
        )

    def _endpoints(self) -> None:
        claims = self._claims()
        self._send(HTTPStatus.OK, endpoint_manifest(self.state.workspace, claims))

    def _fleet(self) -> None:
        claims = self._claims()
        if claims.role not in ("Reviewer", "Admin"):
            raise PolicyError("fleet status requires the Reviewer or Admin role")
        self._send(HTTPStatus.OK, self.state.fleet_status())

    def _ingress(self) -> None:
        """Data-only submission. No code, no compute, no filesystem handle."""
        claims = self._claims()
        body = self._read_json()
        envelope = airlock.IngressEnvelope(
            kind=str(body.get("kind", "")),
            org_id=str(body.get("orgId", "")),
            repo_id=allocations.slugify_repo(str(body.get("repoId", ""))),
            payload=body.get("payload") or {},
            content_hash=str(body.get("contentHash") or ""),
            signature_ref=str(body.get("signatureRef") or ""),
        )
        claims.assert_tenant(envelope.org_id, envelope.repo_id)
        record = self.state.airlock.screen(envelope, claims.tenancy)
        record["subject"] = claims.subject
        queued, disposition = self.state.queue.enqueue(record)
        self._send(
            HTTPStatus.ACCEPTED,
            {
                "disposition": disposition,
                "queued": queued,
                "contentHash": record["contentHash"],
                "tier": record["tier"],
                "queueDepth": self.state.queue.depth(),
            },
        )

    def _modules_list(self) -> None:
        all_modules = [m.to_dict() for m in self.state.module_registry.list_all()]
        self._send(HTTPStatus.OK, {"modules": all_modules, "byChain": self.state.module_registry.by_chain()})

    def _tasks_list(self) -> None:
        params = self._query()
        state_filter = None
        if params.get("state"):
            try:
                state_filter = tasks.TaskState(params["state"].upper())
            except ValueError as exc:
                raise HubCoreError("unknown task state " + repr(params["state"])) from exc
        records = self.state.task_manager.list_all(
            state=state_filter, org_id=params.get("orgId") or None
        )
        self._send(
            HTTPStatus.OK,
            {
                "tasks": [t.to_dict() for t in records],
                "counts": self.state.task_manager.counts_by_state(),
            },
        )

    def _tasks_audit(self) -> None:
        task_id = self._query().get("taskId", "")
        if not task_id:
            raise HubCoreError("taskId query parameter is required")
        trail = self.state.task_manager.audit_trail(task_id)
        if not trail:
            raise HubCoreError("task not found: " + task_id)
        self._send(HTTPStatus.OK, {"taskId": task_id, "trail": trail})

    def _tasks_create(self) -> None:
        body = self._read_json()
        task = self.state.task_manager.create_task(
            kind=str(body.get("kind", "generic")),
            org_id=str(body.get("orgId", self.state.org["orgId"])),
            repo_id=str(body.get("repoId", "main")),
            payload=body.get("payload") or {},
            actor_id=self._actor(),
        )
        self._send(HTTPStatus.CREATED, task.to_dict())

    def _tasks_hold(self) -> None:
        body = self._read_json()
        task = self.state.task_manager.submit_external_result(
            task_id=str(body.get("taskId")),
            result=body.get("result") or {},
            actor_id=self._actor(),
            held_reason=str(body.get("reason", "held for acceptance")),
        )
        self._send(HTTPStatus.OK, task.to_dict())

    def _tasks_accept(self) -> None:
        body = self._read_json()
        task = self.state.task_manager.accept_held_result(
            task_id=str(body.get("taskId")),
            reviewer_id=self._actor(),
        )
        self._send(HTTPStatus.OK, task.to_dict())

    def _tasks_reject(self) -> None:
        body = self._read_json()
        task = self.state.task_manager.reject_held_result(
            task_id=str(body.get("taskId")),
            reviewer_id=self._actor(),
            reason=str(body.get("reason", "rejected by reviewer")),
        )
        self._send(HTTPStatus.OK, task.to_dict())

    def _release_promote(self) -> None:
        body = self._read_json()
        result = self.state.release_engine.promote(
            artifact_rel_path=str(body.get("artifactPath")),
            actor_id=self._actor(),
            notes=str(body.get("notes", "")),
        )
        self._send(HTTPStatus.OK, result)

    # -- Part 9 advisor ---------------------------------------------------

    def _advisor_query(self) -> None:
        """Compatibility surface: a bare question, graded like any other."""
        body = self._read_json()
        response = self.state.advisor_pipeline.submit(
            {
                "body": body.get("query", ""),
                "requestorSubject": self._actor(),
                "module": body.get("module") or {},
                "mode": body.get("mode", "text"),
            }
        )
        self._send(HTTPStatus.OK, response.to_dict())

    def _advisor_submit(self) -> None:
        """The advisor-request/v2 contract (Part 9.1)."""
        response = self.state.advisor_pipeline.submit(self._read_json())
        self._send(HTTPStatus.CREATED, response.to_dict())

    def _advisor_requests(self) -> None:
        params = self._query()
        grade = params.get("grade")
        records = self.state.advisor_pipeline.list_requests(
            state=params.get("state") or None,
            grade=int(grade) if grade not in (None, "") else None,
        )
        self._send(HTTPStatus.OK, {"requests": records, "count": len(records)})

    def _advisor_request_get(self) -> None:
        request_id = self._query().get("requestId", "")
        if not request_id:
            raise HubCoreError("requestId query parameter is required")
        self._send(HTTPStatus.OK, self.state.advisor_pipeline.get(request_id))

    def _advisor_grades(self) -> None:
        self._send(HTTPStatus.OK, {"matrix": advisor.grade_matrix_document()})

    def _advisor_quality(self) -> None:
        self._send(HTTPStatus.OK, self.state.advisor_pipeline.quality_metrics())

    def _advisor_override(self) -> None:
        body = self._read_json()
        self._send(
            HTTPStatus.OK,
            self.state.advisor_pipeline.override_grade(
                request_id=str(body.get("requestId", "")),
                new_grade=body.get("grade"),
                actor_id=self._actor(),
                rationale=str(body.get("rationale", "")),
            ),
        )

    def _advisor_review(self) -> None:
        body = self._read_json()
        self._send(
            HTTPStatus.OK,
            self.state.advisor_pipeline.review(
                request_id=str(body.get("requestId", "")),
                reviewer_id=self._actor(),
                approve=bool(body.get("approve", True)),
                notes=str(body.get("notes", "")),
            ),
        )

    def _advisor_admin_review(self) -> None:
        body = self._read_json()
        self._send(
            HTTPStatus.OK,
            self.state.advisor_pipeline.admin_review(
                request_id=str(body.get("requestId", "")),
                admin_id=self._actor(),
                approve=bool(body.get("approve", True)),
                notes=str(body.get("notes", "")),
            ),
        )

    def _advisor_send(self) -> None:
        body = self._read_json()
        self._send(
            HTTPStatus.OK,
            self.state.advisor_pipeline.send(
                request_id=str(body.get("requestId", "")),
                actor_id=self._actor(),
            ),
        )

    def _advisor_followup(self) -> None:
        body = self._read_json()
        self._send(
            HTTPStatus.OK,
            self.state.advisor_pipeline.follow_up(
                request_id=str(body.get("requestId", "")),
                text=str(body.get("text", "")),
                actor_id=self._actor(),
            ),
        )

    def _advisor_close(self) -> None:
        body = self._read_json()
        self._send(
            HTTPStatus.OK,
            self.state.advisor_pipeline.close(
                request_id=str(body.get("requestId", "")),
                reason=str(body.get("reason", "")),
                actor_id=self._actor(),
            ),
        )

    def _support_ticket(self) -> None:
        """Customer Service Specialist ingress: processes incoming support tickets across the ecosystem.
        
        Tracks all interactions in the Advisor Pipeline and automatically enqueues
        high-impact (Grade >= 4 or escalated) tickets into v-ADMIN for r-ADMIN review.
        """
        body = self._read_json()
        subject = str(body.get("subject", "")).strip()
        # The ticket may name an upstream correspondent, but the governed record
        # is attributed to the authenticated caller that submitted it.
        correspondent = str(body.get("from", "")).strip()
        sender = self._actor()
        content = str(body.get("body", body.get("query", ""))).strip()
        query = f"{subject}\n\n{content}".strip() if subject else content

        response = self.state.advisor_pipeline.submit(
            {
                "body": query,
                "requestorSubject": sender,
                "module": {
                    "id": "customer-service-specialist",
                    "chain": "fulfill",
                    "route": "support-ticket",
                    "metadata": {
                        "from": correspondent or sender,
                        "subject": subject,
                        "messageId": body.get("messageId"),
                        "source": "cloudflare-email-worker",
                    },
                },
                "mode": "text",
            }
        )
        res_dict = response.to_dict()
        res_dict["adminEscalated"] = bool(
            response.grade >= 4 or response.requires_admin or response.escalated
        )
        res_dict["vadminTracking"] = bool(response.admin_synopsis is not None)
        self._send(HTTPStatus.OK, res_dict)

    # -- Part 10.4 v-ADMIN -------------------------------------------------

    def _vadmin_status(self) -> None:
        self._send(HTTPStatus.OK, self.state.vadmin.status())

    def _vadmin_queue(self) -> None:
        include = self._query().get("includeResolved") == "1"
        entries = self.state.vadmin.queue(include_resolved=include)
        self._send(
            HTTPStatus.OK,
            {
                "queue": entries,
                "count": len(entries),
                "disclosure": self.state.vadmin.disclosure,
            },
        )

    def _vadmin_kill_switch(self) -> None:
        body = self._read_json()
        self._send(
            HTTPStatus.OK,
            self.state.vadmin.set_kill_switch(
                engaged=bool(body.get("engaged", True)),
                actor_id=self._actor(),
                reason=str(body.get("reason", "")),
            ),
        )

    def _vadmin_resolve(self) -> None:
        body = self._read_json()
        self._send(
            HTTPStatus.OK,
            self.state.vadmin.resolve(
                entry_id=str(body.get("entryId", "")),
                actor_id=self._actor(),
                resolution=str(body.get("resolution", "")),
            ),
        )

    def _vadmin_proposal(self) -> None:
        body = self._read_json()
        self._send(
            HTTPStatus.ACCEPTED,
            self.state.vadmin.propose_learning(
                proposal=body.get("proposal") or {},
                actor_id=self._actor(),
            ),
        )

    def _expert_ledger(self) -> None:
        recent = self.state.expert_ledger.read_recent()
        self._send(HTTPStatus.OK, {"ledger": recent, "count": len(recent)})

    def _federation_mesh(self) -> None:
        status = self.state.federation.status()
        self._send(HTTPStatus.OK, status)

    def _federation_gradient(self) -> None:
        body = self._read_json()
        res = self.state.federation.ingest_gradient_vector(
            axis=str(body.get("axis", "entirety")),
            sender_node=str(body.get("senderNode", "peer-node")),
            vector=body.get("vector") or [0.0],
            content_hash=str(body.get("contentHash", "")),
        )
        self._send(HTTPStatus.ACCEPTED, res)

    # -- annealing seam ----------------------------------------------------

    def _annealing_status(self) -> None:
        """What the loop has actually forwarded, and how far behind it is."""
        self._send(HTTPStatus.OK, self.state.annealing_status())

    def _annealing_drain(self) -> None:
        """Drain one batch now, instead of waiting for the next period."""
        result = self.state.forwarder.forward_once()
        self._send(
            HTTPStatus.OK,
            {**result.to_dict(), "status": self.state.annealing_status()},
        )

    def _spatial_status(self) -> None:
        self._send(HTTPStatus.OK, self.state.spatial.status())

    def _spatial_preview(self) -> None:
        """The measured constants the Touch axis publishes, before any write."""
        self._send(
            HTTPStatus.OK,
            {"groundTruth": self.state.spatial.preview(), "source": "touch"},
        )

    def _spatial_ground(self) -> None:
        """Register measured metrology as Space-axis reference objects.

        Advisory by construction: this grounds a dimension solve. It moves no
        stock and approves no allocation, so it needs no held-result contract.
        """
        body = self._read_json()
        result = self.state.spatial.ground(dry_run=bool(body.get("dryRun", False)))
        self._send(HTTPStatus.OK, {**result.to_dict(), "dryRun": bool(body.get("dryRun", False))})

    def _spatial_stockopoly_slot(self) -> None:
        """Bridge Stockopoly (:8181) bin coordinates with SAP Storage Location GET metadata."""
        body = self._read_json()
        res = self.state.spatial.stockopoly_slot_lookup(
            sku=str(body.get("sku") or ""),
            dc=str(body.get("dc") or "TX03"),
            sap_location_code=body.get("sapLocationCode"),
            observed_coords=body.get("observedCoords") if isinstance(body.get("observedCoords"), dict) else None,
        )
        self._send(HTTPStatus.OK, res)

    def _mesh_axes(self) -> None:
        """The cognitive axes this node has, with reservations marked as such."""
        report = self.state.fleet_status()
        health = {e["id"]: e["healthy"] for e in report["endpoints"]}
        self._send(HTTPStatus.OK, self.state.axes.summary(health))

    def _bus_status(self) -> None:
        self._send(
            HTTPStatus.OK,
            {
                "ok": True,
                "messageBus": self.state.message_bus.status(),
                "annealing": self.state.annealing_status(),
            },
        )

    def _bus_relay(self) -> None:
        body = self._read_json()
        stream_id = str(body.get("streamId") or "holonhub-live")
        if bool(body.get("pullFromOwner")):
            reg = endpoints.load_registry(self.state.workspace)
            osq_desc = reg.get("osqEngine")
            if osq_desc is None:
                raise HubCoreError("workspace declares no 'osqEngine' endpoint")
            outbox_url = str(body.get("outboxUrl") or (osq_desc.base_url.rstrip("/") + "/api/v1/osq/outbox"))
            result = self.state.message_bus.pull_from_owner(outbox_url, stream_id=stream_id)
            events_for_intake = []
        else:
            raw_events = body.get("events") or []
            if isinstance(body.get("event"), dict) and not raw_events:
                raw_events = [body["event"]]
            result = self.state.message_bus.relay_outbox_events(
                list(raw_events),
                sender_id=stream_id,
                stream_id=stream_id,
            )
            events_for_intake = list(raw_events)

        enqueued_intake = 0
        for ev in events_for_intake:
            if not isinstance(ev, dict):
                continue
            ev_type = str(ev.get("eventType") or "")
            if any(k in ev_type for k in ("rfid", "stockopoly", "quipu", "ifr", "cancel_reconcile", "heuristics")):
                added = self.state.forwarder.enqueue_distilled_telemetry(
                    source="holonhub-live",
                    kind="telemetry.event",
                    org_id=self.state.org["orgId"],
                    summary={
                        "sourceHint": "holonhub-live",
                        "axisHint": "space" if "stockopoly" in ev_type else "brain",
                        "spatialFrame": "ENU_METER_WORLD_V1" if "stockopoly" in ev_type else None,
                        "rfidEdge": "rfid" in ev_type,
                        "fieldCount": len(ev),
                        "confidence": 0.85,
                    },
                    content_hash=str(ev.get("eventId") or f"outbox-{ev.get('seq', 0)}"),
                )
                if added:
                    enqueued_intake += 1
        result["distilledIntakeEnqueued"] = enqueued_intake
        self._send(HTTPStatus.OK, result)

    def _directives_list(self) -> None:
        query = self._query()
        limit = int(query.get("limit") or 50)
        owner_role = query.get("ownerRole") or ""
        status_filter = query.get("status") or ""
        db_path = self.state.library_root / self.state.workspace.get("transactionalStore", {}).get(
            "path", "pipeline/local_brain.sqlite"
        )
        rows = []
        if db_path.exists():
            try:
                import sqlite3 as _sqlite3

                with _sqlite3.connect(str(db_path), timeout=3.0) as conn:
                    conn.row_factory = _sqlite3.Row
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_directives'"
                    ).fetchone()
                    if exists:
                        sql = "SELECT * FROM body_directives WHERE 1=1"
                        params = []
                        if status_filter:
                            sql += " AND status=?"
                            params.append(status_filter)
                        else:
                            sql += " AND status IN ('open','ack','in_progress')"
                        if owner_role:
                            sql += " AND owner_role=?"
                            params.append(owner_role)
                        sql += " ORDER BY priority DESC, id DESC LIMIT ?"
                        params.append(limit)
                        for r in conn.execute(sql, tuple(params)).fetchall():
                            d = dict(r)
                            try:
                                d["evidence"] = json.loads(d.pop("evidence_json", "") or "{}")
                            except Exception:
                                d["evidence"] = {}
                            rows.append(d)
            except Exception:
                pass
        self._send(HTTPStatus.OK, {"ok": True, "count": len(rows), "directives": rows})

    def _directives_feedback(self) -> None:
        body = self._read_json()
        raw_id = body.get("directiveId") or body.get("fingerprint") or ""
        status = str(body.get("status") or "done").strip().lower()
        outcome = str(body.get("outcome") or "").strip()
        if status not in ("ack", "in_progress", "done", "rejected"):
            raise HubCoreError("status must be one of ack, in_progress, done, rejected")
        actor = self._actor()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        db_path = self.state.library_root / self.state.workspace.get("transactionalStore", {}).get(
            "path", "pipeline/local_brain.sqlite"
        )
        updated = False
        if db_path.exists():
            import sqlite3 as _sqlite3

            with _sqlite3.connect(str(db_path), timeout=3.0) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='body_directives'"
                ).fetchone()
                if exists:
                    row = None
                    if str(raw_id).isdigit():
                        row = conn.execute(
                            "SELECT id FROM body_directives WHERE id=?", (int(raw_id),)
                        ).fetchone()
                    if row is None and raw_id:
                        row = conn.execute(
                            "SELECT id FROM body_directives WHERE fingerprint=?", (str(raw_id),)
                        ).fetchone()
                    if row is not None:
                        did = int(row[0])
                        conn.execute(
                            "INSERT INTO body_feedback(directive_id, logged_at, status, outcome, executed_by) VALUES(?,?,?,?,?)",
                            (did, now, status, outcome, actor),
                        )
                        conn.execute(
                            "UPDATE body_directives SET status=?, last_status_at=? WHERE id=?",
                            (status, now, did),
                        )
                        conn.commit()
                        updated = True
        self._send(
            HTTPStatus.OK,
            {
                "ok": True,
                "directiveId": raw_id,
                "status": status,
                "executedBy": actor,
                "updatedInStore": updated,
                "loggedAt": now,
            },
        )

    def _dataplane_watermark(self) -> None:
        body = self._read_json()
        res = self.state.data_plane.export_watermarked_parquet(
            dataset=str(body.get("dataset") or "osq_relational"),
            revision=int(body.get("revision") or 1),
            content_hash=str(body.get("contentHash") or ""),
            outbox_cursor=int(body.get("outboxCursor") or 0),
            rows=body.get("rows") if isinstance(body.get("rows"), list) else None,
        )
        self._send(HTTPStatus.OK, {"ok": True, "watermark": res})

    def _child_hubs_list(self) -> None:
        hubs = [h.to_dict() for h in self.state.child_hubs.list_all()]
        self._send(HTTPStatus.OK, {"childHubs": hubs, "count": len(hubs)})

    def _child_hubs_register(self) -> None:
        body = self._read_json()
        hub_id = str(body.get("id"))
        workers = int(body.get("activeWorkers", 1))
        existing = self.state.child_hubs.get(hub_id)
        if existing:
            updated = self.state.child_hubs.heartbeat(hub_id, workers)
            self._send(HTTPStatus.OK, updated.to_dict())
        else:
            desc = tunnels.ChildHubDescriptor(
                id=hub_id,
                name=str(body.get("name", hub_id)),
                org_id=str(body.get("orgId", "org-child")),
                tunnel_type=str(body.get("tunnelType", "devtunnel")),
                tunnel_endpoint=str(body.get("tunnelEndpoint", "")),
                active_workers=workers,
            )
            self.state.child_hubs.register(desc)
            self._send(HTTPStatus.CREATED, desc.to_dict())

    def _child_hubs_tasks(self) -> None:
        """Tasks a child hub may work on.

        The download is attributed to the hub that asked for it. Crediting a
        download to every registered hub, as an earlier revision did, made the
        per-hub counters describe traffic that never happened.
        """
        hub_id = self._query().get("childHubId", "")
        if hub_id:
            self.state.child_hubs.require(hub_id)
        records = self.state.task_manager.list_all(state=tasks.TaskState.CREATED)
        if hub_id:
            self.state.child_hubs.record_download(hub_id)
        self._send(
            HTTPStatus.OK,
            {
                "tasks": [t.to_dict() for t in records],
                "childHubId": hub_id or None,
                "downloadedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )

    def _child_hubs_proposals(self) -> None:
        body = self._read_json()
        hub_id = str(body.get("childHubId", "unknown-child"))
        task_id = str(body.get("taskId"))
        result = body.get("result") or {}
        reason = str(body.get("reason", f"Proposal submitted by child hub {hub_id}"))

        self.state.child_hubs.record_proposal(hub_id)
        task = self.state.task_manager.submit_external_result(
            task_id=task_id,
            result=result,
            actor_id=f"child:{hub_id}",
            held_reason=reason,
        )
        self._send(HTTPStatus.ACCEPTED, {"status": "held_for_review", "task": task.to_dict()})

    def _child_hubs_session(self) -> None:
        body = self._read_json()
        hub_id = str(body.get("childHubId"))
        user_id = self._actor()
        protocol = str(body.get("protocol", "web-rdp"))
        sess = self.state.rdp_bridge.create_session(hub_id, user_id, protocol)
        self._send(HTTPStatus.CREATED, sess)

    def _marketplace_catalog(self) -> None:
        if not self.state.marketplace:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "marketplace engine unavailable",
                 "detail": self.state.marketplace_error or "not initialised"})
            return
        query = self._query()
        scope = (query.get("scope") or "shared").strip().lower()
        org_id = query.get("org_id")
        # Validate at the policy layer. An invalid scope is a caller error and
        # does not deserve a round trip to the store to find that out.
        if scope not in VALID_MARKETPLACE_SCOPES:
            raise HubCoreError(
                "unknown marketplace scope " + repr(query.get("scope"))
                + "; expected one of " + ", ".join(sorted(VALID_MARKETPLACE_SCOPES))
            )
        if scope == "hub":
            # Hub scope is tenant data. It is never served from this route
            # without a token, whatever the query string asks for.
            _, org_id = self._hub_scope_claims(org_id)
        try:
            assets = self.state.marketplace.get_catalog(
                scope=scope,
                axis=query.get("axis"),
                org_id=org_id,
                domain=query.get("domain"),
                category=query.get("category"),
                limit=int(query.get("limit", 100)),
            )
        except ValueError as exc:
            raise HubCoreError(str(exc)) from exc
        self._send(
            HTTPStatus.OK,
            {"ok": True, "scope": scope, "count": len(assets),
             "assets": [a.to_dict() for a in assets]},
        )

    def _marketplace_routing(self) -> None:
        """Where each scope's information is allowed to travel.

        The scope split is which family of applications the information reaches,
        not a label on a row, so it is inspectable rather than implied.
        """
        from services.marketplace.engine import SCOPE_ROUTING

        self._send(
            HTTPStatus.OK,
            {
                "routing": [
                    {"scope": scope, **{k: list(v) if isinstance(v, tuple) else v
                                        for k, v in spec.items()}}
                    for scope, spec in SCOPE_ROUTING.items()
                ]
            },
        )

    def _marketplace_lakehouse(self) -> None:
        """Tier-2 posture, reported by the process that owns the partitions."""
        self._send(HTTPStatus.OK, self.state.marketplace._call("GET", "/api/v1/marketplace/lakehouse"))

    def _marketplace_hub_catalog(self) -> None:
        if not self.state.marketplace:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "marketplace engine unavailable",
                 "detail": self.state.marketplace_error or "not initialised"})
            return
        query = self._query()
        _, org_id = self._hub_scope_claims(query.get("org_id"))
        assets = self.state.marketplace.get_catalog(
            scope="hub",
            axis=query.get("axis"),
            org_id=org_id,
            domain=query.get("domain"),
            category=query.get("category"),
            limit=int(query.get("limit", 100)),
        )
        self._send(HTTPStatus.OK, {"ok": True, "scope": "hub", "org_id": org_id, "count": len(assets), "assets": [a.to_dict() for a in assets]})

    def _marketplace_hub_stats(self) -> None:
        if not self.state.marketplace:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "marketplace engine unavailable",
                 "detail": self.state.marketplace_error or "not initialised"})
            return
        _, org_id = self._hub_scope_claims(self._query().get("org_id"))
        self._send(HTTPStatus.OK, self.state.marketplace.get_hub_stats(org_id=org_id))

    def _marketplace_stats(self) -> None:
        if not self.state.marketplace:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "marketplace engine unavailable",
                 "detail": self.state.marketplace_error or "not initialised"})
            return
        self._send(HTTPStatus.OK, self.state.marketplace.get_stats())

    def _marketplace_royalties(self) -> None:
        if not self.state.marketplace:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "marketplace engine unavailable",
                 "detail": self.state.marketplace_error or "not initialised"})
            return
        limit = int(self._query().get("limit", 50))
        txs = self.state.marketplace.get_recent_transactions(limit=limit)
        self._send(HTTPStatus.OK, {"ok": True, "count": len(txs), "transactions": txs})

    def _marketplace_tokenize(self) -> None:
        if not self.state.marketplace:
            raise PolicyError("marketplace engine unavailable")
        body = self._read_json()
        asset = self.state.marketplace.tokenize_asset(
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
        self._send(HTTPStatus.CREATED, {"ok": True, "asset": asset.to_dict()})

    def _marketplace_hub_tokenize(self) -> None:
        if not self.state.marketplace:
            raise PolicyError("marketplace engine unavailable")
        body = self._read_json()
        # The organization is taken from the presented token. Reading it from
        # the body let any caller mint into any tenant's hub marketplace.
        _, org_id = self._hub_scope_claims(body.get("org_id"))
        asset = self.state.marketplace.tokenize_supply_chain_asset(
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
        self._send(HTTPStatus.CREATED, {"ok": True, "asset": asset.to_dict()})

    def _marketplace_hub_transfer(self) -> None:
        if not self.state.marketplace:
            raise PolicyError("marketplace engine unavailable")
        body = self._read_json()
        _, org_id = self._hub_scope_claims(body.get("org_id"))
        res = self.state.marketplace.execute_hub_internal_transfer(
            from_user=str(body["from_user"]),
            to_user=str(body["to_user"]),
            asset_id=str(body["asset_id"]),
            shard_count=int(body["shard_count"]),
            reason=str(body.get("reason", "Hub Floor Internal Shard Allocation")),
            org_id=org_id,
        )
        self._send(HTTPStatus.OK, res)

    def _marketplace_hub_sync(self) -> None:
        if not self.state.marketplace:
            raise PolicyError("marketplace engine unavailable")
        body = self._read_json()
        _, org_id = self._hub_scope_claims(body.get("org_id"))
        hub_id = str(body.get("hub_id", "child-hub-01"))
        worker_id = str(body.get("worker_id", "worker-main"))
        local_allocs = body.get("local_allocations", [])
        res = self.state.marketplace.sync_hub_floor_manifest(
            hub_id=hub_id,
            org_id=org_id,
            worker_id=worker_id,
            local_allocations=local_allocs,
        )
        self._send(HTTPStatus.OK, res)

    def _marketplace_trade(self) -> None:
        if not self.state.marketplace:
            raise PolicyError("marketplace engine unavailable")
        body = self._read_json()
        res = self.state.marketplace.execute_trade(
            buyer_id=str(body["buyer_id"]),
            seller_id=str(body["seller_id"]),
            asset_id=str(body["asset_id"]),
            shard_count=int(body["shard_count"]),
            unit_price=float(body["unit_price"]) if "unit_price" in body else None,
            tx_hash=body.get("tx_hash"),
            block_number=body.get("block_number"),
        )
        self._send(HTTPStatus.OK, res)

    def _marketplace_gard_shard(self) -> None:
        if not self.state.marketplace:
            raise PolicyError("marketplace engine unavailable")
        body = self._read_json()
        envelope = self.state.marketplace.create_gard_envelope(
            asset_id=str(body.get("asset_id", "asset-unnamed")),
            payload=body.get("payload", {}),
            shard_count=int(body.get("shard_count", 4)),
            secret=body.get("secret"),
            model_context=body.get("model_context"),
        )
        self._send(HTTPStatus.OK, {"ok": True, "envelope": envelope})

    def _marketplace_gard_verify(self) -> None:
        if not self.state.marketplace:
            raise PolicyError("marketplace engine unavailable")
        body = self._read_json()
        res = self.state.marketplace.verify_and_reassemble_envelope(
            envelope=body.get("envelope", {}),
            secret=body.get("secret"),
        )
        self._send(HTTPStatus.OK, {"ok": True, "result": res})

    def _marketplace_web3_mint(self) -> None:
        if not self.state.marketplace:
            raise PolicyError("marketplace engine unavailable")
        body = self._read_json()
        res = self.state.marketplace.record_on_chain_mint(
            asset_id=str(body["asset_id"]),
            tx_hash=str(body["tx_hash"]),
            contract_address=body.get("contract_address"),
        )
        self._send(HTTPStatus.OK, res)


def _org_is_registered(state: HubCoreState, org_id: str) -> bool:
    tenants = state.workspace.get("allocationSpaces", {}).get("tenants", {})
    return org_id in tenants


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    hub_dir: Optional[Path] = None,
) -> ThreadingHTTPServer:
    """Start the air-lock. Refuses a non-loopback bind unless explicitly forced.

    V10-END-001: wider binding requires authenticated TLS and explicit audit,
    which this stdlib service does not provide, so it fails closed instead.
    """
    if host not in endpoints.LOOPBACK_BINDS:
        import os

        # Inside a container, binding the bridge interface *is* the container
        # bridge case V10-END-001 permits: the published port is the boundary.
        # Anywhere else, a wide bind needs a deliberate, explicit override.
        if not endpoints.running_in_container() and os.environ.get("HUBCORE_ALLOW_WIDE_BIND") != "1":
            raise PolicyError(
                "refusing to bind the air-lock to " + host + "; V10-END-001 requires "
                "loopback or a container bridge. Terminate TLS at an audited reverse "
                "proxy instead."
            )
        logger.warning(
            "binding the air-lock to %s; the publishing boundary is now the "
            "only control on who can reach it", host
        )

    Handler.state = HubCoreState(hub_dir)
    # A served route with no policy entry would be reachable at an undefined
    # access level, which is the failure V10-SEC-001 exists to prevent. Better
    # to refuse to start than to find out later.
    authz.assert_policy_covers(Handler.declared_routes())
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve_forever(server: ThreadingHTTPServer) -> None:
    """Run the service with its background loops attached."""
    Handler.state.forwarder_loop.start()
    try:
        server.serve_forever()
    finally:
        Handler.state.forwarder_loop.stop()


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="HubCore token broker and air-lock")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server = serve(args.host, args.port)
    logger.info("HubCore air-lock listening on http://%s:%s", args.host, args.port)
    logger.info(
        "annealing seam: %s -> %s/observe",
        Handler.state.forwarder.queue_path.name,
        Handler.state.forwarder.observer_url,
    )
    try:
        serve_forever(server)
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
