"""Part 11.2.1 - native endpoint registry, health probes, and bind enforcement.

V10-CFG-003: every connected endpoint declares an immutable descriptor, a health
probe, and an authorization realm. A missing or failing *required* endpoint
fails closed during startup.

V10-END-001: local model endpoints bind to loopback or the container bridge
network and require an authenticated token header for any cross-boundary access.
Direct unauthenticated exposure of a container port to an external interface is
prohibited, and :func:`audit_bindings` reports any host that violates it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .errors import ConfigError, PolicyError

LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Binding a model port here exposes it to every interface on the network.
WILDCARD_BINDS = frozenset({"0.0.0.0", "::", "*"})

DEFAULT_TIMEOUT = 5.0


def running_in_container() -> bool:
    """True when this process is itself inside a container.

    Inside a container, ``127.0.0.1`` is the container -- not the host -- so a
    loopback probe of a sibling service always fails. HubCore then reports a
    healthy fleet as entirely unreachable, which is the most misleading answer
    it can give. Declaring ``serviceHost`` lets the same workspace file work
    from the host and from the bridge network.
    """
    override = os.environ.get("HUBCORE_IN_CONTAINER")
    if override is not None:
        return override == "1"
    return Path("/.dockerenv").exists()


@dataclass
class EndpointDescriptor:
    """One immutable endpoint descriptor from ``hub_workspace.json``."""

    name: str
    id: str
    port: int
    protocol: str = "http"
    health_path: str = "/health"
    axis: Optional[str] = None
    role: str = ""
    bind: str = "127.0.0.1"
    #: Bridge-network name to reach this service by when HubCore is itself
    #: containerized. ``bind`` still describes where the service listens.
    service_host: Optional[str] = None
    realm: str = "core"
    required: bool = False

    @property
    def probe_host(self) -> str:
        """The host HubCore should dial from where it is actually running."""
        if self.service_host and running_in_container():
            return self.service_host
        host = "127.0.0.1" if self.bind in WILDCARD_BINDS or not self.bind else self.bind
        return "[::1]" if host == "::1" else host

    @property
    def base_url(self) -> str:
        return self.protocol + "://" + self.probe_host + ":" + str(self.port)

    @property
    def health_url(self) -> str:
        return self.base_url + "/" + self.health_path.lstrip("/")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HealthResult:
    """The outcome of one health probe."""

    name: str
    id: str
    url: str
    reachable: bool
    status: Optional[int] = None
    latency_ms: Optional[float] = None
    detail: str = ""
    required: bool = False

    @property
    def healthy(self) -> bool:
        return self.reachable and (self.status is None or 200 <= self.status < 400)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["healthy"] = self.healthy
        return payload


def load_registry(workspace: Mapping[str, Any]) -> Dict[str, EndpointDescriptor]:
    """Build the descriptor registry, validating V10-CFG-003 required fields."""
    registry: Dict[str, EndpointDescriptor] = {}
    for name, raw in (workspace.get("endpoints") or {}).items():
        for field in ("id", "port", "healthPath"):
            if field not in raw:
                raise ConfigError(
                    "endpoint " + repr(name) + " is missing " + repr(field) + " (V10-CFG-003)"
                )
        registry[name] = EndpointDescriptor(
            name=name,
            id=str(raw["id"]),
            port=int(raw["port"]),
            protocol=str(raw.get("protocol", "http")),
            health_path=str(raw["healthPath"]),
            axis=raw.get("axis"),
            role=str(raw.get("role", "")),
            bind=str(raw.get("bind", "127.0.0.1")),
            service_host=(str(raw["serviceHost"]) if raw.get("serviceHost") else None),
            realm=str(raw.get("realm", "core")),
            required=bool(raw.get("required", False)),
        )
    if not registry:
        raise ConfigError("workspace declares no endpoints; startup fails closed")
    return registry


def probe(descriptor: EndpointDescriptor, timeout: float = DEFAULT_TIMEOUT) -> HealthResult:
    """Probe one endpoint's health path from wherever HubCore is running."""
    if descriptor.protocol not in ("http", "https"):
        reachable = _tcp_reachable(descriptor.probe_host, descriptor.port, timeout)
        return HealthResult(
            name=descriptor.name,
            id=descriptor.id,
            url=descriptor.base_url,
            reachable=reachable,
            detail="tcp probe" if reachable else "tcp connect refused",
            required=descriptor.required,
        )

    request = urllib.request.Request(
        descriptor.health_url,
        headers={"User-Agent": "HubCore/10.0 health-probe", "Accept": "*/*"},
        method="GET",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            elapsed = (time.perf_counter() - started) * 1000.0
            return HealthResult(
                name=descriptor.name,
                id=descriptor.id,
                url=descriptor.health_url,
                reachable=True,
                status=int(response.status),
                latency_ms=round(elapsed, 2),
                detail="ok",
                required=descriptor.required,
            )
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        return HealthResult(
            name=descriptor.name,
            id=descriptor.id,
            url=descriptor.health_url,
            reachable=True,
            status=int(exc.code),
            latency_ms=round(elapsed, 2),
            detail="http " + str(exc.code),
            required=descriptor.required,
        )
    except Exception as exc:
        return HealthResult(
            name=descriptor.name,
            id=descriptor.id,
            url=descriptor.health_url,
            reachable=False,
            detail=type(exc).__name__ + ": " + str(exc),
            required=descriptor.required,
        )


def _tcp_reachable(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_all(
    workspace: Mapping[str, Any], timeout: float = DEFAULT_TIMEOUT
) -> List[HealthResult]:
    """Probe every declared endpoint concurrently.

    Serial probing made a readiness check cost the *sum* of every endpoint's
    latency, so a fleet with one slow member pushed /health/ready past the
    browser's patience and the UI never rendered a posture at all. Fanning the
    probes out bounds the wall clock at the slowest single endpoint.
    """
    descriptors = list(load_registry(workspace).values())
    if not descriptors:
        return []
    with ThreadPoolExecutor(max_workers=min(len(descriptors), 12)) as pool:
        return list(pool.map(lambda d: probe(d, timeout), descriptors))


def assert_startup_health(
    workspace: Mapping[str, Any], timeout: float = DEFAULT_TIMEOUT
) -> List[HealthResult]:
    """Fail closed if any *required* endpoint is missing or unhealthy.

    Optional endpoints may be down; they surface a disabled reason instead
    (Part 5.2, "Floor may be absent").
    """
    results = probe_all(workspace, timeout)
    failures = [r for r in results if r.required and not r.healthy]
    if failures:
        detail = "; ".join(r.id + " at " + r.url + " (" + r.detail + ")" for r in failures)
        raise PolicyError(
            "required endpoints are unhealthy, startup fails closed (V10-CFG-003): " + detail
        )
    return results


def docker_port_bindings() -> Dict[str, List[str]]:
    """Read the live container port map, if a Docker daemon is reachable."""
    try:
        completed = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception:
        return {}
    bindings: Dict[str, List[str]] = {}
    for line in completed.stdout.splitlines():
        if "\t" not in line:
            continue
        name, ports = line.split("\t", 1)
        bindings[name.strip()] = [p.strip() for p in ports.split(",") if p.strip()]
    return bindings


def audit_bindings(workspace: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Report every endpoint whose live binding violates V10-END-001.

    Returns one finding per offending endpoint. An empty list means the fleet
    binds to loopback or a container bridge only.
    """
    registry = load_registry(workspace)
    live = docker_port_bindings()
    findings: List[Dict[str, Any]] = []
    # A container publishing on both IPv4 and IPv6 reports two mappings for the
    # same exposure; the operator has one thing to fix, so report it once.
    seen: set = set()

    def add(finding: Dict[str, Any]) -> None:
        key = (finding["endpoint"], finding["source"])
        if key in seen:
            return
        seen.add(key)
        findings.append(finding)

    for descriptor in registry.values():
        if descriptor.bind in WILDCARD_BINDS:
            add(
                {
                    "endpoint": descriptor.id,
                    "source": "hub_workspace.json",
                    "binding": descriptor.bind,
                    "requirement": "V10-END-001",
                    "detail": "declared bind exposes the port on every interface",
                }
            )
        for container, ports in live.items():
            for mapping in ports:
                if ":" + str(descriptor.port) + "->" not in mapping.replace(" ", ""):
                    continue
                host = mapping.split("->")[0].rsplit(":", 1)[0].strip("[]")
                if host in WILDCARD_BINDS:
                    add(
                        {
                            "endpoint": descriptor.id,
                            "source": "docker/" + container,
                            "binding": mapping,
                            "requirement": "V10-END-001",
                            "detail": (
                                "container publishes the port on all interfaces; republish "
                                "as 127.0.0.1:" + str(descriptor.port) + ":" + str(descriptor.port)
                            ),
                        }
                    )
    return findings


def status_report(workspace: Mapping[str, Any], timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Combined fleet health and binding audit, safe to show in Floor.

    Never returns a filesystem path, database location, or credential
    (Part 4.6 bridge-status rules).
    """
    results = probe_all(workspace, timeout)
    findings = audit_bindings(workspace)
    return {
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoints": [r.to_dict() for r in results],
        "requiredUnhealthy": [r.id for r in results if r.required and not r.healthy],
        "bindingFindings": findings,
        "compliant": not findings and not any(r.required and not r.healthy for r in results),
    }


def as_json(workspace: Mapping[str, Any]) -> str:
    return json.dumps(status_report(workspace), indent=2)
