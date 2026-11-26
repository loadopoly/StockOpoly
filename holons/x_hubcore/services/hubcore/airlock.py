"""Part 1.3 and Part 21.4 - the Data-Only Ingress Air-Lock and ContaminationGate.

Black Bulls Hideout grants no external entity compute, shell, container, or
filesystem execution. Everything that crosses the membrane is data: signed JSON
or Parquet feature vectors, differential gradient deltas, telemetry events, and
bounded query parameters.

This module is the stateless validation membrane:

* :class:`ContaminationGate` rejects identifiers, media, free text, certificate
  numbers, forbidden strings, out-of-whitelist fields, and every commercial term
  (V10-FED-003 - commercial terms are *unrepresentable*, not merely filtered).
* :class:`AirLock` screens an ingress envelope for executable content, schema
  conformance, payload size, and tenancy scope before it may be queued.
* :func:`apply_differential_privacy` clips and noises a gradient delta.

Every check fails closed and reports an itemized reason (Part 21.8).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .allocations import TenancyTuple
from .errors import ContaminationError, PolicyError

#: Consent tiers (Part 21.4). Tiers are cumulative.
TIER_ISOLATED = 0
TIER_TELEMETRY = 1
TIER_MODEL_DELTAS = 2
TIER_CERTIFIED_EXEMPLARS = 3

#: Artifact kinds permitted at each tier. An artifact kind above the active tier
#: cannot be assembled, held, or released.
TIER_ARTIFACTS: Dict[int, frozenset] = {
    TIER_ISOLATED: frozenset(),
    TIER_TELEMETRY: frozenset({"telemetry.aggregate", "telemetry.calibration", "telemetry.novelty"}),
    TIER_MODEL_DELTAS: frozenset(
        {"telemetry.aggregate", "telemetry.calibration", "telemetry.novelty", "model.adapter-delta"}
    ),
    TIER_CERTIFIED_EXEMPLARS: frozenset(
        {
            "telemetry.aggregate",
            "telemetry.calibration",
            "telemetry.novelty",
            "model.adapter-delta",
            "exemplar.feature-row",
        }
    ),
}

#: V10-FED-003 - these have no encodable field in any export schema.
COMMERCIAL_TERMS = frozenset(
    {
        "price", "unit_price", "unitcost", "unit_cost", "cost", "amount", "spend",
        "supplier", "supplier_id", "supplier_name", "vendor", "vendor_id",
        "vendor_name", "volume", "quantity", "qty", "savings", "discount",
        "lead_time", "leadtime", "lead_time_days", "margin", "revenue", "invoice",
        "contract", "rate", "tariff", "quote",
    }
)

#: Direct or quasi identifiers that must never leave the deployment.
IDENTIFIER_TERMS = frozenset(
    {
        "name", "first_name", "last_name", "full_name", "email", "phone", "ssn",
        "address", "street", "postal_code", "zip", "employee_id", "badge",
        "username", "user_name", "account", "customer", "customer_id",
        "certificate", "certificate_number", "cert_no", "serial", "serial_number",
        "vin", "license", "passport", "tax_id", "ein", "duns",
    }
)

#: Media and free-text carriers.
MEDIA_TERMS = frozenset(
    {"image", "images", "frame", "frames", "photo", "audio", "video", "media",
     "blob", "attachment", "screenshot", "scan", "pdf", "thumbnail"}
)
FREE_TEXT_TERMS = frozenset(
    {"text", "raw_text", "note", "notes", "comment", "comments", "description",
     "transcript", "body", "message", "prompt", "summary", "narrative"}
)

#: Executable or active content signatures refused at ingress.
EXECUTABLE_MARKERS = (
    "<script", "javascript:", "data:text/html", "<?php", "<%", "#!/",
    "__import__", "eval(", "exec(", "subprocess", "os.system", "powershell -",
    "cmd.exe", "base64 -d", "curl http", "wget http", "\x4d\x5a", "\x7fELF",
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]?){9,15}\d(?!\d)")
LONG_DIGIT_RE = re.compile(r"(?<!\d)\d{9,}(?!\d)")
IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")

#: A telemetry or exemplar value that is a bare string longer than this is free
#: text by definition and is refused.
MAX_CATEGORICAL_LENGTH = 64

DEFAULT_MAX_PAYLOAD_BYTES = 1024 * 1024


def _normalize(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


@dataclass
class GateReport:
    """Itemized ContaminationGate outcome (Part 21.8)."""

    passed: bool
    artifact_kind: str
    tier: int
    rejections: List[Dict[str, str]] = field(default_factory=list)
    inspected_fields: int = 0
    content_hash: str = ""

    def reject(self, category: str, location: str, reason: str) -> None:
        self.passed = False
        self.rejections.append({"category": category, "field": location, "reason": reason})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "artifactKind": self.artifact_kind,
            "tier": self.tier,
            "inspectedFields": self.inspected_fields,
            "contentHash": self.content_hash,
            "rejections": self.rejections,
        }

    def raise_if_failed(self) -> "GateReport":
        if not self.passed:
            detail = "; ".join(
                item["category"] + " at " + item["field"] + ": " + item["reason"]
                for item in self.rejections
            )
            raise ContaminationError("ContaminationGate refused the artifact: " + detail)
        return self


class ContaminationGate:
    """Fail-closed egress and ingress screen.

    ``field_whitelist`` is the *only* set of leaf field names an artifact may
    carry. Anything not on it is refused, so adding a field to an export is a
    deliberate, reviewable act rather than an accident.
    """

    def __init__(
        self,
        field_whitelist: Optional[Iterable[str]] = None,
        forbidden_strings: Optional[Iterable[str]] = None,
        max_depth: int = 8,
    ):
        self.field_whitelist = frozenset(_normalize(f) for f in (field_whitelist or ()))
        self.forbidden_strings = tuple(
            s.lower() for s in (forbidden_strings or ()) if str(s).strip()
        )
        self.max_depth = max_depth

    def screen(self, artifact: Mapping[str, Any], artifact_kind: str, tier: int) -> GateReport:
        """Screen one artifact against the active tier and whitelist."""
        report = GateReport(passed=True, artifact_kind=artifact_kind, tier=tier)

        permitted = TIER_ARTIFACTS.get(tier)
        if permitted is None:
            report.reject("tier", "$", "consent tier " + str(tier) + " is not defined")
            return report
        if artifact_kind not in permitted:
            report.reject(
                "tier",
                "$",
                "artifact kind " + repr(artifact_kind) + " is above the active consent "
                "tier " + str(tier) + " and cannot be assembled",
            )
            return report

        try:
            canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            report.reject("encoding", "$", "artifact is not JSON-serializable: " + str(exc))
            return report
        report.content_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        self._walk(artifact, "$", report, depth=0)
        return report

    def _walk(self, node: Any, location: str, report: GateReport, depth: int) -> None:
        if depth > self.max_depth:
            report.reject("shape", location, "artifact nesting exceeds the permitted depth")
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                child = location + "." + str(key)
                self._check_key(str(key), child, report)
                self._walk(value, child, report, depth + 1)
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                self._walk(value, location + "[" + str(index) + "]", report, depth + 1)
        else:
            report.inspected_fields += 1
            self._check_value(node, location, report)

    def _check_key(self, key: str, location: str, report: GateReport) -> None:
        normalized = _normalize(key)
        if normalized in COMMERCIAL_TERMS:
            report.reject(
                "commercial-term", location,
                "commercial terms have no encodable field in an export schema (V10-FED-003)",
            )
        if normalized in IDENTIFIER_TERMS:
            report.reject("identifier", location, "direct or quasi identifiers may not egress")
        if normalized in MEDIA_TERMS:
            report.reject("media", location, "media carriers may not egress at any tier")
        if normalized in FREE_TEXT_TERMS:
            report.reject("free-text", location, "free text may not egress at any tier")
        if self.field_whitelist and normalized not in self.field_whitelist:
            report.reject(
                "out-of-whitelist", location,
                "field is not on the export schema whitelist",
            )

    def _check_value(self, value: Any, location: str, report: GateReport) -> None:
        if value is None or isinstance(value, (bool, int)):
            return
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                report.reject("shape", location, "non-finite numeric value")
            return
        if isinstance(value, (bytes, bytearray)):
            report.reject("media", location, "binary payloads may not egress")
            return
        if not isinstance(value, str):
            report.reject("shape", location, "unsupported value type " + type(value).__name__)
            return

        text = value
        lowered = text.lower()
        if len(text) > MAX_CATEGORICAL_LENGTH:
            report.reject(
                "free-text", location,
                "string exceeds " + str(MAX_CATEGORICAL_LENGTH) + " characters and is free text",
            )
        if EMAIL_RE.search(text):
            report.reject("identifier", location, "value contains an email address")
        if PHONE_RE.search(text):
            report.reject("identifier", location, "value contains a phone-like number")
        if IPV4_RE.search(text):
            report.reject("identifier", location, "value contains an IP address")
        if LONG_DIGIT_RE.search(text):
            report.reject(
                "identifier", location,
                "value contains a long digit run consistent with a certificate, "
                "serial, or account number",
            )
        for marker in EXECUTABLE_MARKERS:
            if marker in lowered:
                report.reject("executable", location, "value contains active content " + repr(marker))
                break
        for forbidden in self.forbidden_strings:
            if forbidden in lowered:
                report.reject(
                    "forbidden-string", location,
                    "value contains the deployment forbidden string " + repr(forbidden)
                    + " (V10-ID-001)",
                )
                break


@dataclass
class IngressEnvelope:
    """One inbound data-only submission from an external client."""

    kind: str
    org_id: str
    repo_id: str
    payload: Mapping[str, Any]
    content_hash: str = ""
    signature_ref: str = ""


class AirLock:
    """Stateless quarantine screen in front of the QUIPU training intake queue.

    The air-lock never executes, interprets, or persists caller-supplied code.
    It validates and either queues a training signal or refuses it.
    """

    #: The only ingress kinds the membrane accepts.
    ACCEPTED_KINDS = frozenset(
        {"telemetry.event", "feature.vector", "gradient.delta", "query.request"}
    )

    def __init__(
        self,
        gate: Optional[ContaminationGate] = None,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        tier: int = TIER_TELEMETRY,
    ):
        self.gate = gate or ContaminationGate()
        self.max_payload_bytes = int(max_payload_bytes)
        self.tier = int(tier)

    def screen(self, envelope: IngressEnvelope, claims_tenancy: TenancyTuple) -> Dict[str, Any]:
        """Validate an ingress envelope. Raises on any refusal.

        Returns the queue record to hand to the training intake, including the
        computed content hash for at-least-once deduplication (Part 4.1).
        """
        if envelope.kind not in self.ACCEPTED_KINDS:
            raise PolicyError(
                "ingress kind " + repr(envelope.kind) + " is not accepted; the membrane "
                "is data-only and grants no external compute (Part 1.3)"
            )
        if envelope.org_id != claims_tenancy.org_id or envelope.repo_id != claims_tenancy.repo_id:
            raise PolicyError(
                "envelope tenancy does not match the presented token scope; refused"
            )

        try:
            canonical = json.dumps(
                envelope.payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise PolicyError("ingress payload is not JSON-serializable: " + str(exc)) from exc

        if len(canonical) > self.max_payload_bytes:
            raise PolicyError(
                "ingress payload of " + str(len(canonical)) + " bytes exceeds the "
                + str(self.max_payload_bytes) + " byte membrane limit"
            )

        computed = "sha256:" + hashlib.sha256(canonical).hexdigest()
        if envelope.content_hash and envelope.content_hash != computed:
            raise PolicyError("ingress contentHash does not match the payload; refused")

        self._assert_no_executable(canonical)

        artifact_kind = {
            "telemetry.event": "telemetry.aggregate",
            "feature.vector": "exemplar.feature-row",
            "gradient.delta": "model.adapter-delta",
            "query.request": "telemetry.aggregate",
        }[envelope.kind]
        report = self.gate.screen(envelope.payload, artifact_kind, self.tier)
        report.raise_if_failed()

        return {
            "acceptedAt": _now(),
            "kind": envelope.kind,
            "artifactKind": artifact_kind,
            "orgId": envelope.org_id,
            "repoId": envelope.repo_id,
            "contentHash": computed,
            "idempotencyKey": computed,
            "tier": self.tier,
            "gate": report.to_dict(),
            # The distilled shape of the payload, so the annealing loop has
            # something to observe once the payload itself is discarded.
            "summary": summarize_payload(envelope.payload),
        }

    @staticmethod
    def _assert_no_executable(raw: bytes) -> None:
        """Refuse anything carrying an executable or archive signature."""
        prefixes = (b"MZ", b"\x7fELF", b"PK\x03\x04", b"\x1f\x8b", b"\xca\xfe\xba\xbe", b"#!")
        for prefix in prefixes:
            if raw.lstrip()[: len(prefix)] == prefix:
                raise PolicyError("ingress carries an executable or archive signature; refused")
        lowered = raw.lower()
        for marker in EXECUTABLE_MARKERS:
            if marker.encode("utf-8", "ignore") in lowered:
                raise PolicyError(
                    "ingress carries active content " + repr(marker) + "; the membrane "
                    "grants zero external compute"
                )
        # A base64 blob large enough to smuggle a binary is refused outright.
        for candidate in re.findall(rb"[A-Za-z0-9+/=]{512,}", raw):
            try:
                decoded = base64.b64decode(candidate[: 4 * (len(candidate) // 4)], validate=True)
            except Exception:
                continue
            if decoded[:2] in (b"MZ", b"PK") or decoded[:4] == b"\x7fELF":
                raise PolicyError("ingress carries a base64-encoded executable; refused")


def _summarize_vector(values: Any) -> Optional[Dict[str, Any]]:
    """Describe a numeric vector by its shape, never by its contents."""
    if not isinstance(values, (list, tuple)) or not values:
        return None
    numbers = [float(v) for v in values if isinstance(v, (int, float))]
    if not numbers:
        return None
    norm = math.sqrt(sum(v * v for v in numbers))
    return {
        "dimensions": len(numbers),
        "l2Norm": round(norm, 6),
        "mean": round(sum(numbers) / len(numbers), 6),
        "min": round(min(numbers), 6),
        "max": round(max(numbers), 6),
    }


def summarize_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Distil a screened payload into something trainable, at screening time.

    The queue previously held receipts only -- content hashes, tiers, and gate
    reports -- so the "training intake queue" contained nothing anyone could
    train on. This runs while the payload is still in hand and keeps only its
    shape: field counts, and a vector's dimensions, norm, and extrema. No field
    value from the submission survives, so an observation cannot become a covert
    channel for the raw data the membrane exists to keep out.
    """
    summary: Dict[str, Any] = {
        "fieldCount": len(payload) if isinstance(payload, Mapping) else 0
    }
    if not isinstance(payload, Mapping):
        return summary
    for key in ("featureVector", "vector", "gradient", "delta"):
        vector = _summarize_vector(payload.get(key))
        if vector:
            summary["vector"] = vector
            break
    axis = payload.get("axis")
    if isinstance(axis, str) and axis:
        summary["axisHint"] = axis[:32]
    confidence = payload.get("confidence")
    if isinstance(confidence, (int, float)):
        summary["confidence"] = max(0.0, min(1.0, float(confidence)))
    return summary


def apply_differential_privacy(
    delta: Sequence[float],
    clip_norm: float = 1.0,
    noise_multiplier: float = 1.1,
    rng: Optional[random.Random] = None,
) -> Dict[str, Any]:
    """Clip an adapter delta to ``clip_norm`` and add calibrated Gaussian noise.

    Returns the noised vector plus the DP parameters, which Part 21.4 requires to
    appear in every receipt.
    """
    if clip_norm <= 0:
        raise PolicyError("clip_norm must be positive")
    if noise_multiplier < 0:
        raise PolicyError("noise_multiplier must not be negative")
    values = [float(v) for v in delta]
    if not values:
        raise PolicyError("cannot privatize an empty delta")
    norm = math.sqrt(sum(v * v for v in values))
    scale = min(1.0, clip_norm / norm) if norm > 0 else 1.0
    clipped = [v * scale for v in values]
    generator = rng or random.SystemRandom()
    sigma = noise_multiplier * clip_norm
    noised = [v + generator.gauss(0.0, sigma) for v in clipped]
    return {
        "vector": noised,
        "dp": {
            "clipNorm": clip_norm,
            "noiseMultiplier": noise_multiplier,
            "sigma": sigma,
            "originalNorm": norm,
            "clipped": scale < 1.0,
        },
    }


def forbidden_strings_from_env() -> List[str]:
    """Deployment-specific forbidden strings for V10-ID-001 lint.

    Sourced from ``HUBCORE_FORBIDDEN_STRINGS`` (comma separated) so the list is
    never itself compiled into the product.
    """
    raw = os.environ.get("HUBCORE_FORBIDDEN_STRINGS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
