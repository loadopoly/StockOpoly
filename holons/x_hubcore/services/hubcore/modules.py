"""Part 3 - Seven Chains, Modules Registry, and Sandbox Policy.

V10-MOD-001: Every user-facing module declares an immutable manifest, a target
chain slot, declared capabilities, and runtime permissions.
V10-MOD-002: Modules execute with least-privilege sandboxing. AI or external
workers cannot escalate authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set

from . import config
from .errors import ConfigError, PolicyError

SEVEN_CHAINS = (
    "Orchestrate",
    "Plan",
    "Order",
    "Source",
    "Transform",
    "Fulfill",
    "Return",
)

ALLOWED_CAPABILITIES = frozenset({
    "data.read",
    "data.query",
    "ai.infer",
    "ui.embed",
    "export.view",
    "telemetry.emit",
})

FORBIDDEN_CAPABILITIES = frozenset({
    "release.promote",
    "prod.write",
    "archive.write",
    "policy.write",
    "shell.execute",
    "secret.read",
})


@dataclass
class ModuleManifest:
    """Descriptor and permission boundary for one Hub module."""

    id: str
    name: str
    version: str
    chain: str
    entrypoint: str
    description: str = ""
    icon: str = "box"
    capabilities: List[str] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)
    enabled: bool = True

    def validate(self) -> None:
        if not self.id or not self.name or not self.version:
            raise ConfigError("module is missing id, name, or version")
        if self.chain not in SEVEN_CHAINS and self.chain != "General":
            raise ConfigError(
                f"module {self.id} specifies unknown chain '{self.chain}'; "
                f"expected one of {', '.join(SEVEN_CHAINS)}"
            )
        for cap in self.capabilities:
            if cap in FORBIDDEN_CAPABILITIES:
                raise PolicyError(
                    f"module {self.id} requests forbidden authority capability: {cap}"
                )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ModuleRegistry:
    """Manages registered modules, chain assignments, and execution dispatch."""

    def __init__(self, modules_doc: Optional[Mapping[str, Any]] = None):
        self._modules: Dict[str, ModuleManifest] = {}
        if modules_doc is None:
            documents = config.load_all()
            modules_doc = documents.get("modules", {})
        self._load(modules_doc)

    def _load(self, doc: Mapping[str, Any]) -> None:
        raw_list = doc.get("modules", [])
        for raw in raw_list:
            mod_id = str(raw.get("moduleId") or raw.get("id", ""))
            chain_val = str(raw.get("chain") or raw.get("chainId", "General")).capitalize()
            manifest = ModuleManifest(
                id=mod_id,
                name=str(raw.get("name", mod_id)),
                version=str(raw.get("version", "1.0.0")),
                chain=chain_val,
                entrypoint=str(raw.get("entrypoint", "index.html")),
                description=str(raw.get("description", "")),
                icon=str(raw.get("icon", "box")),
                capabilities=list(raw.get("capabilities", [])),
                permissions=list(raw.get("permissions", [])),
                enabled=bool(raw.get("enabled", True)),
            )
            manifest.validate()
            self._modules[manifest.id] = manifest

    def get(self, module_id: str) -> Optional[ModuleManifest]:
        return self._modules.get(module_id)

    def list_all(self, enabled_only: bool = False) -> List[ModuleManifest]:
        if enabled_only:
            return [m for m in self._modules.values() if m.enabled]
        return list(self._modules.values())

    def by_chain(self) -> Dict[str, List[Dict[str, Any]]]:
        result: Dict[str, List[Dict[str, Any]]] = {c: [] for c in SEVEN_CHAINS}
        result["General"] = []
        for m in self._modules.values():
            target = m.chain if m.chain in result else "General"
            result[target].append(m.to_dict())
        return result

    def register(self, manifest: ModuleManifest) -> None:
        manifest.validate()
        self._modules[manifest.id] = manifest
