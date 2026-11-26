"""Part 5 & Part 21 - Child Hubs Attachment, Tunnel Manager & Bounded RDP Bridge.

V10-TUN-001: Child hubs and external worker nodes attach through authenticated
tunnels (DevTunnel, Cloudflare, Loopback). Remote sessions remain strictly bounded
within the application perimeter.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from . import config, tasks
from .errors import PolicyError


@dataclass
class ChildHubDescriptor:
    """Descriptor for an attached external child hub or satellite node."""

    id: str
    name: str
    org_id: str
    tunnel_type: str  # devtunnel | cloudflare | loopback | direct
    tunnel_endpoint: str
    rdp_enabled: bool = True
    status: str = "active"  # active | idle | disconnected
    active_workers: int = 1
    capabilities: List[str] = field(default_factory=lambda: ["floor.download", "proposal.submit"])
    last_seen: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    downloaded_tasks_count: int = 0
    proposals_submitted_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ChildHubRegistry:
    """Child hubs that have actually attached to this node.

    The registry starts empty. A hub appears only after it registers, and its
    reported status decays to ``stale`` once it stops sending heartbeats --
    a mesh view that invents members, or calls a silent one "active", tells an
    operator the opposite of what they opened it to find out.
    """

    #: A hub that has not been heard from within this window is not active.
    HEARTBEAT_TTL_SECONDS = 300

    def __init__(self, workspace: Optional[Dict[str, Any]] = None):
        if workspace is None:
            self.workspace = config.load("workspace")
        else:
            self.workspace = workspace
        self._hubs: Dict[str, ChildHubDescriptor] = {}
        self._lock = threading.RLock()

    def register(self, descriptor: ChildHubDescriptor) -> None:
        with self._lock:
            self._hubs[descriptor.id] = descriptor

    def heartbeat(self, hub_id: str, active_workers: Optional[int] = None) -> ChildHubDescriptor:
        with self._lock:
            hub = self._hubs.get(hub_id)
            if not hub:
                raise PolicyError("child hub '" + hub_id + "' is not registered")
            hub.last_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            hub.status = "active"
            if active_workers is not None:
                hub.active_workers = active_workers
            return hub

    def _age_seconds(self, hub: ChildHubDescriptor) -> float:
        try:
            seen = calendar.timegm(time.strptime(hub.last_seen, "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError):
            return float("inf")
        return max(0.0, time.time() - seen)

    def _with_liveness(self, hub: ChildHubDescriptor) -> ChildHubDescriptor:
        if hub.status != "disconnected" and self._age_seconds(hub) > self.HEARTBEAT_TTL_SECONDS:
            hub.status = "stale"
        return hub

    def list_all(self) -> List[ChildHubDescriptor]:
        with self._lock:
            return [self._with_liveness(hub) for hub in self._hubs.values()]

    def get(self, hub_id: str) -> Optional[ChildHubDescriptor]:
        with self._lock:
            hub = self._hubs.get(hub_id)
            return self._with_liveness(hub) if hub else None

    def require(self, hub_id: str) -> ChildHubDescriptor:
        hub = self.get(hub_id)
        if hub is None:
            raise PolicyError(
                "child hub '" + hub_id + "' is not registered on this node; it "
                "must attach through /api/v1/hubs/children/register first"
            )
        return hub

    def record_download(self, hub_id: str) -> None:
        with self._lock:
            hub = self._hubs.get(hub_id)
            if hub:
                hub.downloaded_tasks_count += 1

    def record_proposal(self, hub_id: str) -> None:
        """Count a proposal against a *registered* hub, or refuse it."""
        with self._lock:
            hub = self._hubs.get(hub_id)
            if not hub:
                raise PolicyError(
                    "child hub '" + hub_id + "' is not registered; a proposal "
                    "from an unattached node cannot be attributed"
                )
            hub.proposals_submitted_count += 1


class BoundedRdpBridge:
    """Coordinates in-application bounded remote access and terminal sessions."""

    #: Registry consulted before a session is minted, when one is supplied.
    def __init__(
        self,
        workspace: Optional[Dict[str, Any]] = None,
        registry: Optional["ChildHubRegistry"] = None,
    ):
        self.workspace = workspace or config.load("workspace")
        self.registry = registry
        self._lock = threading.RLock()
        self._active_sessions: Dict[str, Dict[str, Any]] = {}

    def create_session(
        self, child_hub_id: str, user_id: str, protocol: str = "web-rdp"
    ) -> Dict[str, Any]:
        """Mint a bounded session descriptor for an attached child hub.

        A session for a hub that never attached is meaningless, so it is
        refused rather than minted: the caller gets a policy error instead of a
        session id that can never connect to anything.
        """
        if protocol not in ("web-rdp", "web-terminal"):
            raise PolicyError("unsupported bounded session protocol: " + protocol)
        if self.registry is not None:
            self.registry.require(child_hub_id)

        session_id = "rdp-sess-" + uuid.uuid4().hex[:12]
        session = {
            "sessionId": session_id,
            "childHubId": child_hub_id,
            "userId": user_id,
            "protocol": protocol,
            # The session secret is returned in the body, never embedded in a
            # path or query string where proxies and logs would capture it.
            "sessionToken": "bd-rdp-" + uuid.uuid4().hex,
            "streamPath": "/api/v1/hubs/children/" + child_hub_id + "/stream",
            "bounded": True,
            "sandboxFrame": "strict-origin",
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "expiresAt": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)
            ),
        }
        with self._lock:
            self._active_sessions[session_id] = session
        return session

    def validate_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._active_sessions
