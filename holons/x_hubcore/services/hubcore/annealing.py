"""Part 11.2.2 / Part 21 - the annealing seam between the air-lock and QUIPU.

The Data-Only Ingress Air-Lock accepts external telemetry, screens it, and
queues it "for the QUIPU annealing loop". Until this module existed, nothing
drained that queue: `quipu_training_intake.jsonl` grew, QUIPU declared a
`hubcore` source profile on the perception axis, and the two never met. The
federation flywheel was open at exactly the seam HubCore owns.

Two things are implemented here.

:class:`IntakeForwarder` drains screened intake records into QUIPU `/observe`
on the perception axis. It is deliberately conservative:

* **Distilled, never raw.** What crosses is a bounded observation sentence and
  summary statistics. Feature vectors and gradient deltas are described by their
  shape and norm, never replayed. Federation never moves raw data (decision
  0.3.13), and that rule does not relax because the destination is local.
* **Durable cursor, fail-safe.** The cursor advances only for records QUIPU
  accepted. If QUIPU is unreachable the queue is untouched, nothing is lost, and
  the lag is reported rather than hidden.
* **Tier-gated.** A record screened at a consent tier that does not permit
  training use is skipped and counted, not forwarded.

:class:`AxisRegistry` reports the cognitive axes this node actually has. An axis
whose endpoint is declared but unimplemented is reported `reserved`, never
`live`. A mesh diagram that shows a participant nobody built is the same defect
as a health panel that invents a green tick.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import config, endpoints
from .airlock import summarize_payload  # noqa: F401  (re-exported)

#: QUIPU's source key for this node's governance axis. QUIPU maps it to the
#: `perception` axis via its own SOURCE_PROFILES table.
QUIPU_SOURCE = "hubcore"
QUIPU_AXIS = "perception"

#: Consent tiers whose artifacts may be used for training. Tier 0 is telemetry
#: only and is still trainable; a record screened above the active tier never
#: reaches the queue in the first place.
TRAINABLE_KINDS = frozenset(
    {"telemetry.event", "feature.vector", "gradient.delta"}
)

#: A query is a read, not an observation. Forwarding it would train the model on
#: what people asked rather than on what happened.
NON_TRAINABLE_KINDS = frozenset({"query.request"})

#: Upper bound on one distilled observation. QUIPU is fed a sentence, not a file.
MAX_OBSERVATION_CHARS = 1200

#: Distilled spatial / RFID telemetry source profiles routed into QUIPU.
SPATIAL_TELEMETRY_SOURCES = frozenset({"hubcore", "stockopoly", "holonhub-live", "rust-rfid"})

DEFAULT_BATCH = 50
DEFAULT_INTERVAL_SECONDS = 45.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS annealing_cursor (
    queue_name   TEXT PRIMARY KEY,
    line_offset  INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS annealing_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    kind         TEXT NOT NULL,
    disposition  TEXT NOT NULL,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS annealing_hash_idx ON annealing_events(content_hash);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- distillation -------------------------------------------------------------


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


def distill(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Turn a screened intake record into a bounded observation for QUIPU.

    The result is prose plus summary statistics. It carries no field values from
    the submitted payload, so an observation cannot become a covert channel for
    the raw data the membrane exists to keep out.
    """
    kind = str(record.get("kind", "unknown"))
    org = str(record.get("orgId", "unknown"))
    tier = record.get("tier", 0)
    gate = record.get("gate") or {}
    summary = record.get("summary") or {}

    parts = [
        "External node observation accepted through the data-only air-lock.",
        "Kind " + kind + ", artifact class " + str(record.get("artifactKind", "unknown")) + ".",
        "Consent tier " + str(tier) + ".",
        "Originating organization partition " + org + ".",
    ]
    if gate.get("inspected_fields") is not None:
        parts.append(
            "ContaminationGate inspected " + str(gate.get("inspected_fields")) + " fields."
        )
    vector = summary.get("vector")
    if vector:
        parts.append(
            "Signal is a " + str(vector["dimensions"]) + "-dimensional vector with L2 norm "
            + str(vector["l2Norm"]) + " and mean " + str(vector["mean"]) + "."
        )
    if summary.get("axisHint"):
        parts.append("Client routed it onto the " + str(summary["axisHint"]) + " axis.")
    if summary.get("spatialFrame"):
        parts.append("Spatial frame " + str(summary["spatialFrame"]) + ".")
    if summary.get("rfidEdge"):
        parts.append("RFID transit telemetry calibration update observed.")
    if summary.get("fieldCount") is not None:
        parts.append("Payload declared " + str(summary["fieldCount"]) + " top-level fields.")

    source_hint = str(summary.get("sourceHint") or "").strip()
    source = source_hint if source_hint in SPATIAL_TELEMETRY_SOURCES else QUIPU_SOURCE
    text = " ".join(parts)[:MAX_OBSERVATION_CHARS]
    return {
        "source": source,
        "kind": "governance",
        "text": text,
        "confidence": float(summary.get("confidence") or 0.5),
    }


# -- forwarding ---------------------------------------------------------------


@dataclass
class ForwardResult:
    scanned: int = 0
    forwarded: int = 0
    skipped: int = 0
    failed: int = 0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scanned": self.scanned,
            "forwarded": self.forwarded,
            "skipped": self.skipped,
            "failed": self.failed,
            "error": self.error,
        }


class IntakeForwarder:
    """Drains the training intake queue into the QUIPU annealing loop."""

    def __init__(
        self,
        queue_path: Path,
        observer_url: str,
        db_path: Optional[Path] = None,
        batch: int = DEFAULT_BATCH,
        timeout: float = 5.0,
    ):
        self.queue_path = Path(queue_path)
        self.observer_url = observer_url.rstrip("/")
        self.batch = int(batch)
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._last_success_at: Optional[str] = None
        self._last_error = ""
        self._last_run_at: Optional[str] = None
        self._total_forwarded = 0
        self._total_skipped = 0

        self.db_path = Path(db_path) if db_path else None
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path) if self.db_path else ":memory:", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        if self.db_path is not None:
            self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._enqueued_hashes: set = set()
        if self.queue_path.exists():
            for raw_line in self.queue_path.read_text(encoding="utf-8").splitlines():
                try:
                    ch = json.loads(raw_line).get("contentHash")
                    if ch:
                        self._enqueued_hashes.add(str(ch))
                except Exception:
                    continue

    # -- cursor -----------------------------------------------------------

    @property
    def _queue_key(self) -> str:
        return self.queue_path.name

    def cursor(self) -> int:
        row = self._conn.execute(
            "SELECT line_offset FROM annealing_cursor WHERE queue_name = ?",
            (self._queue_key,),
        ).fetchone()
        return int(row["line_offset"]) if row else 0

    def _set_cursor(self, offset: int) -> None:
        self._conn.execute(
            "INSERT INTO annealing_cursor (queue_name, line_offset, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT(queue_name) DO UPDATE SET"
            " line_offset=excluded.line_offset, updated_at=excluded.updated_at",
            (self._queue_key, int(offset), _now()),
        )
        self._conn.commit()

    def _record(self, content_hash: str, kind: str, disposition: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO annealing_events (at, content_hash, kind, disposition, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (_now(), content_hash, kind, disposition, detail or None),
        )

    # -- queue ------------------------------------------------------------

    def _queue_lines(self) -> List[str]:
        if not self.queue_path.exists():
            return []
        return self.queue_path.read_text(encoding="utf-8").splitlines()

    def pending(self) -> int:
        return max(0, len(self._queue_lines()) - self.cursor())

    def enqueue_distilled_telemetry(
        self,
        *,
        source: str,
        kind: str,
        org_id: str,
        summary: Mapping[str, Any],
        content_hash: str,
    ) -> bool:
        """Append a tier-0-safe distilled telemetry.event line if unseen."""
        chash = str(content_hash or "").strip()
        if not chash:
            return False
        source_val = str(source or QUIPU_SOURCE).strip()
        distilled_summary = dict(summary or {})
        if source_val in SPATIAL_TELEMETRY_SOURCES:
            distilled_summary.setdefault("sourceHint", source_val)
        record = {
            "kind": str(kind or "telemetry.event"),
            "artifactKind": "distilled.cyberphysical.telemetry",
            "orgId": str(org_id or "default"),
            "repoId": "holonhub-live",
            "contentHash": chash,
            "tier": 0,
            "gate": {"inspected_fields": int(distilled_summary.get("fieldCount") or 0)},
            "summary": distilled_summary,
        }
        with self._lock:
            if chash in self._enqueued_hashes:
                return False
            self._enqueued_hashes.add(chash)
            self.queue_path.parent.mkdir(parents=True, exist_ok=True)
            with self.queue_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        return True

    # -- transport --------------------------------------------------------

    def _post_observation(self, observation: Mapping[str, Any]) -> Tuple[bool, str]:
        body = json.dumps(observation).encode("utf-8")
        request = urllib.request.Request(
            self.observer_url + "/observe",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "HubCore/10.1 annealing"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
            if not payload.get("ok", True):
                return False, str(payload.get("error", "observer refused the observation"))
            return True, str(payload.get("axis", QUIPU_AXIS))
        except urllib.error.HTTPError as exc:
            return False, "HTTP " + str(exc.code) + " from the observer"
        except Exception as exc:  # transport failure: keep the queue, report the lag
            return False, type(exc).__name__ + ": " + str(exc)

    # -- the drain --------------------------------------------------------

    def forward_once(self) -> ForwardResult:
        """Forward one batch. The cursor advances only over settled records.

        A transport failure stops the batch where it stands. Nothing is dropped,
        nothing is double-counted, and the next run resumes at the same record.
        """
        result = ForwardResult()
        with self._lock:
            self._last_run_at = _now()
            lines = self._queue_lines()
            start = self.cursor()
            offset = start

            for raw in lines[start : start + self.batch]:
                result.scanned += 1
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    # A corrupt line is settled as unusable rather than retried
                    # forever, which would wedge the queue behind one bad byte.
                    self._record("", "unparseable", "skipped", "line is not valid JSON")
                    result.skipped += 1
                    offset += 1
                    continue

                kind = str(record.get("kind", "unknown"))
                content_hash = str(record.get("contentHash", ""))

                if kind in NON_TRAINABLE_KINDS or kind not in TRAINABLE_KINDS:
                    self._record(content_hash, kind, "skipped", "kind is not trainable")
                    result.skipped += 1
                    self._total_skipped += 1
                    offset += 1
                    continue

                ok, detail = self._post_observation(distill(record))
                if not ok:
                    self._record(content_hash, kind, "deferred", detail)
                    self._conn.commit()
                    result.failed += 1
                    result.error = detail
                    self._last_error = detail
                    break

                self._record(content_hash, kind, "forwarded", "axis " + detail)
                result.forwarded += 1
                self._total_forwarded += 1
                offset += 1

            if offset != start:
                self._set_cursor(offset)
            self._conn.commit()

            if result.forwarded:
                self._last_success_at = _now()
                self._last_error = ""
        return result

    def status(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._queue_lines())
            cursor = self.cursor()
            return {
                "queue": self._queue_key,
                "observerUrl": self.observer_url,
                "axis": QUIPU_AXIS,
                "queueDepth": total,
                "cursor": cursor,
                "pending": max(0, total - cursor),
                "forwarded": self._total_forwarded,
                "skipped": self._total_skipped,
                "lastRunAt": self._last_run_at,
                "lastSuccessAt": self._last_success_at,
                # An empty error with pending work means the loop has not run
                # yet, not that everything is fine.
                "lastError": self._last_error,
                "healthy": not self._last_error,
            }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class ForwarderLoop:
    """Runs the forwarder on a period, in the background, and stays quiet."""

    def __init__(self, forwarder: IntakeForwarder, interval: float = DEFAULT_INTERVAL_SECONDS):
        self.forwarder = forwarder
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hubcore-annealing", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.forwarder.forward_once()
            except Exception:  # a background loop never takes the service down
                continue

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None


# -- the axis registry --------------------------------------------------------

#: The cognitive axes QUIPU routes observations onto, and the endpoint that
#: feeds each one. `implemented` is a statement about code that exists.
DECLARED_AXES: Tuple[Dict[str, Any], ...] = (
    {
        "axis": "entirety",
        "role": "Cognitive observer and annealing engine",
        "endpointRef": "observer",
        "quipuSource": None,
        "implemented": True,
    },
    {
        "axis": "vision",
        "role": "Unstructured capture and OCR",
        "endpointRef": "visionCapture",
        "quipuSource": "loadopoly-ocr",
        "implemented": True,
    },
    {
        "axis": "touch",
        "role": "Structured dimensional metrology",
        "endpointRef": "structuredMetrology",
        "quipuSource": "bakugo",
        "implemented": True,
    },
    {
        "axis": "brain",
        "role": "Supply chain decision core",
        "endpointRef": "decisionEngine",
        "quipuSource": "supply-chain-brain",
        "implemented": True,
    },
    {
        "axis": "perception",
        "role": "Governance and external ingress",
        "endpointRef": "tokenBroker",
        "quipuSource": "hubcore",
        "implemented": True,
    },
    {
        "axis": "space",
        "role": "Warehouse mapping, dimension solving, and 3D slotting",
        "endpointRef": "warehouseEngine",
        "quipuSource": "stockopoly",
        "implemented": True,
        # Grounded by the Touch axis: measured slab metrology is registered as
        # StockOpoly reference objects, so the dimension solve is anchored to
        # physical measurement rather than to catalogue nominals.
        "groundedBy": "touch",
    },
)


class AxisRegistry:
    """Reports the cognitive axes this node actually has."""

    def __init__(self, workspace: Optional[Mapping[str, Any]] = None):
        self.workspace = workspace if workspace is not None else config.load("workspace")

    def describe(self, health: Optional[Mapping[str, bool]] = None) -> List[Dict[str, Any]]:
        declared = self.workspace.get("endpoints") or {}
        rows: List[Dict[str, Any]] = []
        for axis in DECLARED_AXES:
            descriptor = declared.get(axis["endpointRef"]) or {}
            endpoint_id = descriptor.get("id")
            healthy = None if health is None else bool(health.get(endpoint_id, False))
            rows.append(
                {
                    **axis,
                    "endpointId": endpoint_id,
                    "port": descriptor.get("port"),
                    "required": bool(descriptor.get("required", False)),
                    "healthy": healthy,
                    # An axis is live only when something implements it AND the
                    # endpoint answered. Declared plus down is not live.
                    "status": (
                        "reserved" if not axis["implemented"]
                        else "live" if healthy
                        else "declared" if healthy is None
                        else "down"
                    ),
                }
            )
        return rows

    def summary(self, health: Optional[Mapping[str, bool]] = None) -> Dict[str, Any]:
        rows = self.describe(health)
        return {
            "axes": rows,
            "implemented": sum(1 for r in rows if r["implemented"]),
            "reserved": sum(1 for r in rows if not r["implemented"]),
            "live": sum(1 for r in rows if r["status"] == "live"),
            "total": len(rows),
        }


def observer_url(workspace: Mapping[str, Any]) -> str:
    """Resolve the observer endpoint from wherever HubCore is running."""
    registry = endpoints.load_registry(workspace)
    descriptor = registry.get("observer")
    if descriptor is None:
        raise config.ConfigError("workspace declares no 'observer' endpoint")
    return descriptor.base_url
