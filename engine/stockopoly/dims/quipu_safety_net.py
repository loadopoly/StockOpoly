"""Separable Safety Net for QUIPU Observer calls.

Provides:
1. Circuit Breaker:
   - States: CLOSED (normal), OPEN (tripped), HALF_OPEN (probing).
   - Fast timeout (2.5s) prevents solver or server threads from hanging on QUIPU.
   - Trips on consecutive errors/timeouts (default 3); recovers after backoff (20s).
2. Decoupled Persistent Spool / Outbox:
   - Spools observations to ``quipu_safety_spool.jsonl`` with UUID idempotency keys.
   - Zero caller blockage: spooled calls immediately return success.
3. Planck Displacement Filter:
   - Evaluates whether new observation carries real informational displacement
     (|ΔRMS| > ε or variable change). Zero-displacement ticks record a lightweight
     heartbeat, saving Landauer bit erasures and electrical energy.
4. Separable Asynchronous Drain Worker:
   - Runs as an independent background daemon or on-demand manual flush.
   - Drains pending spooled payloads to QUIPU when circuit recovers.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15.0          # Safe timeout in seconds for QUIPU graph percolation & world model
_CIRCUIT_FAIL_THRESHOLD = 3      # Consecutive failures before opening circuit
_RECOVERY_WINDOW_S = 20.0        # Time in OPEN state before trying HALF_OPEN
_PLANCK_EPSILON = 1e-4           # Displacement resolution limit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SafetyNetMetrics:
    total_calls: int = 0
    direct_success: int = 0
    spooled_diverted: int = 0
    drained_success: int = 0
    planck_gated_heartbeats: int = 0
    consecutive_failures: int = 0
    circuit_state: str = "CLOSED"    # CLOSED | OPEN | HALF_OPEN
    last_tripped_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None


class QuipuSafetyNet:
    """Separable, decoupled safety net wrapping all calls to the QUIPU Observer."""

    def __init__(
        self,
        spool_path: Optional[Path] = None,
        enabled: bool = True,
        fast_timeout: float = _DEFAULT_TIMEOUT,
        fail_threshold: int = _CIRCUIT_FAIL_THRESHOLD,
        recovery_window_s: float = _RECOVERY_WINDOW_S,
        planck_eps: float = _PLANCK_EPSILON,
    ):
        self.enabled = enabled
        self.fast_timeout = float(fast_timeout)
        self.fail_threshold = int(fail_threshold)
        self.recovery_window_s = float(recovery_window_s)
        self.planck_eps = float(planck_eps)

        if spool_path is None:
            engine_dir = Path(__file__).resolve().parent.parent.parent  # StockOpoly/engine
            self.spool_path = engine_dir / "data" / "quipu_safety_spool.jsonl"
        else:
            self.spool_path = Path(spool_path)

        self._lock = threading.RLock()
        self.metrics = SafetyNetMetrics()
        self._last_state_vector: Optional[dict[str, float]] = None
        self._circuit_opened_time: float = 0.0

    # ───────────────────────────────────────────────────────── Circuit Management
    def _check_circuit(self) -> str:
        """Evaluate circuit breaker state machine."""
        with self._lock:
            if self.metrics.circuit_state == "OPEN":
                now_mono = time.monotonic()
                if now_mono - self._circuit_opened_time >= self.recovery_window_s:
                    self.metrics.circuit_state = "HALF_OPEN"
                    logger.info("QuipuSafetyNet: Circuit transitioned from OPEN to HALF_OPEN (probing)")
            return self.metrics.circuit_state

    def _record_success(self) -> None:
        with self._lock:
            self.metrics.consecutive_failures = 0
            self.metrics.last_success_at = _now()
            if self.metrics.circuit_state == "HALF_OPEN":
                self.metrics.circuit_state = "CLOSED"
                logger.info("QuipuSafetyNet: Probe succeeded. Circuit CLOSED.")

    def _record_failure(self, err: str) -> None:
        with self._lock:
            self.metrics.consecutive_failures += 1
            self.metrics.last_error = err
            if self.metrics.consecutive_failures >= self.fail_threshold and self.metrics.circuit_state != "OPEN":
                self.metrics.circuit_state = "OPEN"
                self._circuit_opened_time = time.monotonic()
                self.metrics.last_tripped_at = _now()
                logger.warning(
                    "QuipuSafetyNet: Circuit TRIPPED to OPEN after %d consecutive failures. Last error: %s",
                    self.metrics.consecutive_failures,
                    err,
                )

    # ───────────────────────────────────────────────────────── Planck Gate Check
    def is_displaced(self, payload: dict[str, Any]) -> bool:
        """Check if payload represents true informational displacement."""
        readout = (
            payload.get("meta", {})
            .get("relational", {})
            .get("readout", {})
        )
        rms = float(readout.get("r", 0.0))
        control = float(readout.get("control", 0.0))

        with self._lock:
            if self._last_state_vector is None:
                self._last_state_vector = {"rms": rms, "control": control}
                return True

            delta_rms = abs(rms - self._last_state_vector["rms"])
            delta_ctrl = abs(control - self._last_state_vector["control"])
            displacement = math.sqrt(delta_rms ** 2 + delta_ctrl ** 2)

            if displacement > self.planck_eps:
                self._last_state_vector = {"rms": rms, "control": control}
                return True
            return False

    # ───────────────────────────────────────────────────────── Spool Persistence
    def spool(self, target_url: str, payload: dict[str, Any], reason: str = "circuit_open") -> str:
        """Append observation to safety spool file."""
        key = str(uuid.uuid4())
        record = {
            "idempotency_key": key,
            "created_at": _now(),
            "target_url": target_url,
            "reason": reason,
            "attempts": 0,
            "payload": payload,
        }
        with self._lock:
            try:
                self.spool_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.spool_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, default=str) + "\n")
                self.metrics.spooled_diverted += 1
            except Exception as exc:
                logger.error("QuipuSafetyNet: Failed to write to spool %s: %s", self.spool_path, exc)
        return key

    # ───────────────────────────────────────────────────────── Protected Dispatch
    def execute(
        self,
        target_url: str,
        payload: dict[str, Any],
        raw_dispatcher: Callable[[str, dict[str, Any], float], Tuple[bool, Any]],
    ) -> dict[str, Any]:
        """Execute observation dispatch wrapped in separable safety net."""
        with self._lock:
            self.metrics.total_calls += 1

        if not self.enabled:
            # Separable bypass: safety net disabled
            ok, res = raw_dispatcher(target_url, payload, self.fast_timeout)
            return {"ok": ok, "response": res, "safety_net": "disabled"}

        # 1. Planck displacement check
        if not self.is_displaced(payload):
            with self._lock:
                self.metrics.planck_gated_heartbeats += 1
            return {
                "ok": True,
                "planck_gated": True,
                "safety_net": "heartbeat_retained",
                "message": "Zero informational displacement. Landauer energy conserved.",
            }

        # 2. Circuit Breaker check
        state = self._check_circuit()
        if state == "OPEN":
            key = self.spool(target_url, payload, reason="circuit_breaker_open")
            return {
                "ok": True,
                "spooled": True,
                "safety_net": "diverted_open_circuit",
                "circuit": "OPEN",
                "idempotency_key": key,
            }

        # 3. Direct dispatch with fast timeout
        try:
            ok, res = raw_dispatcher(target_url, payload, self.fast_timeout)
            if ok:
                self._record_success()
                with self._lock:
                    self.metrics.direct_success += 1
                return {"ok": True, "response": res, "safety_net": "direct_pass"}
            else:
                err_str = str(res)
                self._record_failure(err_str)
                key = self.spool(target_url, payload, reason=f"dispatch_failed: {err_str}")
                return {
                    "ok": True,
                    "spooled": True,
                    "safety_net": "diverted_on_failure",
                    "error": err_str,
                    "idempotency_key": key,
                }
        except Exception as exc:
            err_str = f"{type(exc).__name__}: {exc}"
            self._record_failure(err_str)
            key = self.spool(target_url, payload, reason=f"dispatch_exception: {err_str}")
            return {
                "ok": True,
                "spooled": True,
                "safety_net": "diverted_on_exception",
                "error": err_str,
                "idempotency_key": key,
            }

    # ───────────────────────────────────────────────────────── Drain / Flush
    def flush(
        self,
        raw_dispatcher: Callable[[str, dict[str, Any], float], Tuple[bool, Any]],
        max_batch: int = 50,
    ) -> dict[str, Any]:
        """Drain spooled records to QUIPU."""
        if not self.spool_path.exists():
            return {"drained": 0, "remaining": 0, "ok": True}

        with self._lock:
            try:
                lines = self.spool_path.read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if not lines:
            return {"drained": 0, "remaining": 0, "ok": True}

        remaining_records = []
        drained_count = 0
        errors = []

        for line in lines:
            if not line.strip():
                continue
            if drained_count >= max_batch:
                remaining_records.append(line)
                continue

            try:
                rec = json.loads(line)
            except Exception:
                continue

            url = rec.get("target_url")
            payload = rec.get("payload")
            ok, res = raw_dispatcher(url, payload, self.fast_timeout)
            if ok:
                drained_count += 1
                with self._lock:
                    self.metrics.drained_success += 1
                self._record_success()
            else:
                rec["attempts"] = rec.get("attempts", 0) + 1
                rec["last_error"] = str(res)
                remaining_records.append(json.dumps(rec))
                errors.append(str(res))
                self._record_failure(str(res))
                # Stop batch if QUIPU is rejecting
                break

        with self._lock:
            try:
                if remaining_records:
                    self.spool_path.write_text("\n".join(remaining_records) + "\n", encoding="utf-8")
                else:
                    self.spool_path.unlink(missing_ok=True)
            except Exception as exc:
                errors.append(f"spool_write_err: {exc}")

        return {
            "ok": len(errors) == 0,
            "drained": drained_count,
            "remaining": len(remaining_records),
            "errors": errors,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            spool_depth = 0
            if self.spool_path.exists():
                try:
                    spool_depth = sum(1 for line in open(self.spool_path, "r", encoding="utf-8") if line.strip())
                except Exception:
                    pass
            m = asdict(self.metrics)
            m["spool_depth"] = spool_depth
            m["enabled"] = self.enabled
            m["spool_path"] = str(self.spool_path)
            return m


# Global default instance
_DEFAULT_SAFETY_NET: Optional[QuipuSafetyNet] = None


def get_safety_net() -> QuipuSafetyNet:
    global _DEFAULT_SAFETY_NET
    if _DEFAULT_SAFETY_NET is None:
        _DEFAULT_SAFETY_NET = QuipuSafetyNet()
    return _DEFAULT_SAFETY_NET
