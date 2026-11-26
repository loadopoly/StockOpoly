"""Part 11 - AI Runtime and Provider Studio Adapter.

V10-PRV-001: Model execution complies with HAL tier constraints (<=4 GB heavy model
budget in Tier 1) and provides automatic fallback to local endpoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from . import config, hal
from .errors import PolicyError


@dataclass
class ProviderDescriptor:
    id: str
    name: str
    kind: str  # local-container | local-process | remote-api
    endpoint: str
    model_name: str
    max_context: int
    vram_cost_mb: int
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProviderStudio:
    """Manages AI provider routing, fallbacks, and VRAM budget guardrails."""

    def __init__(self, workspace: Optional[Dict[str, Any]] = None):
        if workspace is None:
            self.workspace = config.load("workspace")
        else:
            self.workspace = workspace
        self.profile = hal.probe(self.workspace)
        self._providers: Dict[str, ProviderDescriptor] = {}
        self._init_default_providers()

    def _init_default_providers(self) -> None:
        # Default local fleet providers matching registered descriptors
        self.register(
            ProviderDescriptor(
                id="quipu-observer",
                name="QUIPU Observer & Annealer",
                kind="local-container",
                endpoint="http://127.0.0.1:7100",
                model_name="quipu-annealer-v10",
                max_context=16384,
                vram_cost_mb=2048,
            )
        )
        self.register(
            ProviderDescriptor(
                id="loadopoly-ocr",
                name="Loadopoly Vision OCR",
                kind="local-container",
                endpoint="http://127.0.0.1:3000",
                model_name="geograph-ocr-v2",
                max_context=8192,
                vram_cost_mb=1024,
            )
        )
        self.register(
            ProviderDescriptor(
                id="bakugo-metrology",
                name="Bakugo CardCenter Metrology",
                kind="local-container",
                endpoint="http://127.0.0.1:8765",
                model_name="bakugo-tesseract-v2",
                max_context=4096,
                vram_cost_mb=512,
            )
        )

    def register(self, provider: ProviderDescriptor) -> None:
        if provider.vram_cost_mb > self.profile.heavy_model_budget_mb:
            raise PolicyError(
                f"provider {provider.id} requires {provider.vram_cost_mb} MB VRAM, "
                f"which exceeds the HAL Tier {self.profile.tier} budget of {self.profile.heavy_model_budget_mb} MB"
            )
        self._providers[provider.id] = provider

    def list_all(self) -> List[ProviderDescriptor]:
        return list(self._providers.values())

    def get(self, provider_id: str) -> Optional[ProviderDescriptor]:
        return self._providers.get(provider_id)
