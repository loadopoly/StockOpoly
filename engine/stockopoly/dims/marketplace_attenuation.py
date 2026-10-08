"""Marketplace Attachment Attenuation Engine — modulates connection and gauge
dynamics based on GARD Shard interactions.

Theoretical formulation:
1. Fisher Information & Cramér-Rao Bounds:
   Each solved metrology entity carries Fisher Information I(θ) = J^T W J
   and Cramér-Rao lower bound CRB = σ_ln^2.
2. GARD Shard Interaction Envelope:
   When an entity is tokenized into DCC1-WLS and sealed into authenticated
   gard-shard/v2 envelopes under domain 'SiCi_SQRT(-1)':
   - Authentication Score S_auth ∈ [0.50, 1.00]
   - Mean CRB bound across active axes: CRB_mean = (1/k) Σ σ_ln^2
3. Shard Attenuation Factor α_shard:
   α_shard = S_auth / (1.0 + λ_crb * CRB_mean)
   bounded in [0.10, 1.00].
4. Connection Attenuation:
   - Gauge Prior Sigma: σ_gauge = σ_0 * w_phase * α_shard
     (High shard confidence tightens prior; high CRB variance relaxes/attenuates).
   - Dispatch Velocity: V_dispatch = V_nominal * α_shard
     (Throttles downstream token emission during noisy measurement regimes).
   - Shadow Price Margin: dampens ERP transfer price updates to protect against
     volatile metrology noise.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_LAMBDA_CRB = 0.50       # Sensitivity of attenuation to Cramér-Rao Bound
_MIN_ATTENUATION = 0.10          # Maximum damping floor
_MAX_ATTENUATION = 1.00          # Undamped ceiling


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ShardInteractionRecord:
    timestamp: str
    entity_id: int
    token_id: str
    domain: str
    authenticated: bool
    fisher_info_mean: float
    crb_mean: float
    alpha_shard: float
    dimensions: dict[str, float]


class MarketplaceAttenuationEngine:
    """Manages connection attenuation derived from GARD Shard interactions."""

    def __init__(self, lambda_crb: float = _DEFAULT_LAMBDA_CRB):
        self.lambda_crb = float(lambda_crb)
        self._lock = threading.RLock()
        self._current_alpha: float = 1.0
        self._last_interaction_at: Optional[str] = None
        self._total_shards_processed: int = 0
        self._history: list[ShardInteractionRecord] = []
        self._max_history = 100

    def compute_shard_attenuation(
        self,
        crb_mean: float,
        authenticated: bool = True,
        fisher_mean: float = 0.0,
    ) -> float:
        """Calculate the dimensionless attenuation factor α_shard in [0.10, 1.00]."""
        s_auth = 1.00 if authenticated else 0.60
        safe_crb = max(0.0, min(100.0, crb_mean))
        denom = 1.0 + (self.lambda_crb * safe_crb)
        alpha = s_auth / denom
        return round(max(_MIN_ATTENUATION, min(_MAX_ATTENUATION, alpha)), 4)

    def record_shard_interaction(
        self,
        entity_id: int,
        token_id: str,
        authenticated: bool,
        fisher_info_dict: dict[str, float],
        crb_dict: dict[str, float],
        dimensions: dict[str, float],
    ) -> float:
        """Ingest a completed GARD Shard transaction and update attenuation state."""
        finite_crbs = [v for v in crb_dict.values() if math.isfinite(v) and v >= 0]
        mean_crb = sum(finite_crbs) / len(finite_crbs) if finite_crbs else 1.0

        finite_fishers = [v for v in fisher_info_dict.values() if math.isfinite(v)]
        mean_fisher = sum(finite_fishers) / len(finite_fishers) if finite_fishers else 1.0

        alpha = self.compute_shard_attenuation(
            crb_mean=mean_crb,
            authenticated=authenticated,
            fisher_mean=mean_fisher,
        )

        rec = ShardInteractionRecord(
            timestamp=_now(),
            entity_id=entity_id,
            token_id=token_id,
            domain="SiCi_SQRT(-1)",
            authenticated=authenticated,
            fisher_info_mean=round(mean_fisher, 4),
            crb_mean=round(mean_crb, 4),
            alpha_shard=alpha,
            dimensions=dimensions,
        )

        with self._lock:
            self._current_alpha = alpha
            self._last_interaction_at = rec.timestamp
            self._total_shards_processed += 1
            self._history.append(rec)
            if len(self._history) > self._max_history:
                self._history.pop(0)

        logger.debug(
            "MarketplaceAttenuation: Ingested shard %s (eid=%d). CRB=%.4f, Fisher=%.4f -> Alpha=%.4f",
            token_id,
            entity_id,
            mean_crb,
            mean_fisher,
            alpha,
        )
        return alpha

    def attenuate_gauge_sigma(self, base_sigma: float, w_phase: float = 1.0) -> float:
        """Modulate the WLS gauge prior sigma using the current shard attenuation factor."""
        with self._lock:
            alpha = self._current_alpha
        # Attenuation modulates the gauge: when precision is high (alpha ~ 1.0), gauge prior is nominal;
        # when uncertainty/CRB is high (alpha < 1.0), gauge prior is scaled to prevent over-constraining.
        return round(base_sigma * w_phase * alpha, 6)

    def attenuate_velocity(self, nominal_velocity: float) -> float:
        """Attenuate transaction or dispatch velocity."""
        with self._lock:
            alpha = self._current_alpha
        return round(nominal_velocity * alpha, 4)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "current_alpha_attenuation": self._current_alpha,
                "total_shards_processed": self._total_shards_processed,
                "last_interaction_at": self._last_interaction_at,
                "lambda_crb": self.lambda_crb,
                "history_count": len(self._history),
                "recent_records": [asdict(r) for r in self._history[-5:]],
            }


# Global default instance
_DEFAULT_ATTENUATION_ENGINE: Optional[MarketplaceAttenuationEngine] = None


def get_attenuation_engine() -> MarketplaceAttenuationEngine:
    global _DEFAULT_ATTENUATION_ENGINE
    if _DEFAULT_ATTENUATION_ENGINE is None:
        _DEFAULT_ATTENUATION_ENGINE = MarketplaceAttenuationEngine()
    return _DEFAULT_ATTENUATION_ENGINE
