"""Part 21 - Tri-Repo Mesh Federation (Cross-Organization Learning).

V10-FED-001: Federation exchanges only distilled, privacy-preserved gradients and
state packs. Raw tenant records never cross boundaries.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from . import airlock, config, endpoints
from .errors import PolicyError


@dataclass
class FederatedSignal:
    axis: str  # entirety | vision | touch
    sender_node: str
    clip_norm: float
    noise_applied: bool
    gradient_dim: int
    content_hash: str
    timestamp: str


class FederationCoordinator:
    """Coordinates tri-repo multi-axis annealing and federated mesh signals."""

    def __init__(
        self,
        workspace: Optional[Dict[str, Any]] = None,
        org: Optional[Dict[str, Any]] = None,
    ):
        if workspace is None:
            self.workspace = config.load("workspace")
        else:
            self.workspace = workspace
        self.org = org if org is not None else config.load("org")
        self.lock_cfg = self.workspace.get("airlock", {})
        self.dp_cfg = self.lock_cfg.get("differentialPrivacy", {"clipNorm": 1.0, "noiseMultiplier": 1.1})
        self._history: List[FederatedSignal] = []

    def ingest_gradient_vector(
        self,
        axis: str,
        sender_node: str,
        vector: List[float],
        content_hash: str,
    ) -> Dict[str, Any]:
        if axis not in ("entirety", "vision", "touch"):
            raise PolicyError(f"invalid federation axis: {axis}")

        # Apply Differential Privacy: L2 clipping
        clip_norm = float(self.dp_cfg.get("clipNorm", 1.0))
        noise_mult = float(self.dp_cfg.get("noiseMultiplier", 1.1))

        l2 = math.sqrt(sum(x * x for x in vector)) or 1.0
        scale = min(1.0, clip_norm / l2)
        clipped = [x * scale for x in vector]

        # Add Gaussian noise
        sigma = (clip_norm * noise_mult) / math.sqrt(len(vector) or 1)
        noised = [x + random.gauss(0, sigma) for x in clipped]

        sig = FederatedSignal(
            axis=axis,
            sender_node=sender_node,
            clip_norm=clip_norm,
            noise_applied=True,
            gradient_dim=len(noised),
            content_hash=content_hash,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._history.append(sig)

        return {
            "status": "accepted_for_annealing",
            "axis": axis,
            "dimensions": len(noised),
            "contentHash": content_hash,
            "signal": asdict(sig),
        }

    def status(self) -> Dict[str, Any]:
        return {
            # Federation is a consent decision recorded in hub_org.json, not a
            # side effect of having a transport endpoint configured (Part 21).
            "federationEnabled": bool(self.org.get("federation", {}).get("enabled")),
            "consentTier": int(self.org.get("federation", {}).get("consentTier", 0)),
            "axes": ["entirety", "vision", "touch"],
            "dpParameters": self.dp_cfg,
            "signalsIngested": len(self._history),
            "recentSignals": [asdict(s) for s in self._history[-10:]],
        }
