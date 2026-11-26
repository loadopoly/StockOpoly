"""Part 7.1 tier 2 - the analytical lakehouse and blob store for the marketplace.

The marketplace store was described as "atomic SQLite WAL transactional store and
DuckDB Parquet audit trails". Only the first half existed: `data_dir` was created
and never written to, and the directory held zero files. This module is the
second half.

The split follows Part 7.1, by mutability rather than by preference:

* **SQLite WAL keeps mutable current state** -- `tokenized_assets`,
  `shard_holdings`, `pending_rewards`. A shard balance is a read-modify-write
  under concurrency, and Parquet is immutable columnar; doing a decrement against
  it means rewriting a file and hoping nobody interleaved.
* **Parquet keeps append-only history** -- royalty transactions, hub floor sync
  records, and envelope metadata. Unbounded growth, never updated, queried
  analytically. Part 7.1 calls the lakehouse "the authoritative holding location
  for all historical operational data".
* **The blob store keeps payloads.** A GARD envelope carries base64 shard
  ciphertext inline; a 10 KB plaintext already produces a 2 KB envelope, and a
  scan or a metrology capture would put megabytes of base64 into a TEXT column.
  Payload bytes live in content-addressed blobs and the row keeps the digest.

Two rules the old code broke, restated here because they are the point:

`V10-DAT-003`: A missing analytical engine is a **refusal**, not a mocked row.
The previous fallback returned ``[{"warning": "duckdb not installed; ..."}]`` --
a result set shaped exactly like an answer, which a caller iterating rows cannot
distinguish from one.

`V10-DAT-004`: Exactly one process writes a Parquet partition. Parquet has no
locking at all, so two appenders to one directory interleave partial files with
nothing to arbitrate them. Ownership is a deployment property and is asserted at
startup, not assumed.
"""

from __future__ import annotations

import datetime
import decimal
import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

#: Append-only datasets that belong in the lakehouse, and the column each one is
#: partitioned by. Hive partitioning keeps a scan from reading the whole history
#: to answer a question about one month or one organization.
DATASETS: Dict[str, Dict[str, Any]] = {
    "royalty_transactions": {
        "partition_by": ("event_month",),
        "description": "Royalty distribution events, 50/30/20 community/holder/maintenance",
    },
    "hub_floor_sync_records": {
        "partition_by": ("org_id", "event_month"),
        "description": "Child hub manifest reconciliations",
    },
    "gard_envelopes": {
        "partition_by": ("event_month",),
        "description": "GARD shard envelope metadata; payload bytes live in the blob store",
    },
}

MANIFEST_VERSION = 10
BLOB_DIRNAME = "_blobs"


class LakehouseUnavailable(RuntimeError):
    """Raised when the analytical engine is absent. Never silently mocked."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _month(timestamp: Optional[str] = None) -> str:
    stamp = timestamp or _now()
    return stamp[:7] if len(stamp) >= 7 else time.strftime("%Y-%m", time.gmtime())


def duckdb_module():
    """Import DuckDB, or refuse in a way that names the missing dependency."""
    try:
        import duckdb  # noqa: F401
    except ImportError as exc:
        raise LakehouseUnavailable(
            "the analytical tier needs duckdb and it is not installed in this "
            "image; install it or query the Parquet sinks from a host that has "
            "it. There is no mocked result: a fabricated row is indistinguishable "
            "from a real one (V10-DAT-003)"
        ) from exc
    return duckdb


def _json_safe(value: Any) -> Any:
    """Coerce an engine value into something a JSON response can carry.

    DuckDB hands back `datetime`, `date`, and `Decimal` for the column types it
    infers. Every one of these rows is on its way to an HTTP response, so
    leaving them raw turns a successful query into a serialisation error at the
    edge -- far from the code that caused it.
    """
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


# -- blob store ---------------------------------------------------------------


@dataclass(frozen=True)
class BlobRef:
    """A content-addressed payload, stored beside the lakehouse."""

    digest: str
    size_bytes: int
    media_type: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "digest": self.digest,
            "sizeBytes": self.size_bytes,
            "mediaType": self.media_type,
        }


class BlobStore:
    """Content-addressed local blob storage (Part 1.1, Native Governed Data Plane).

    Content addressing is not decoration: an envelope's shard ciphertext is
    immutable once sealed, so the digest *is* the identity. Two writers storing
    the same bytes converge on one file instead of racing, which is the only
    reason it is safe for this store to be written from more than one place.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, digest: str) -> Path:
        # Two-level fan-out; a flat directory of a million blobs is a bad time.
        return self.root / digest[:2] / digest[2:4] / digest

    def put(self, data: bytes, media_type: str = "application/octet-stream") -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        target = self._path_for(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename: a reader never sees a partial blob.
            temporary = target.with_suffix(".part")
            temporary.write_bytes(data)
            os.replace(temporary, target)
        return BlobRef(digest=digest, size_bytes=len(data), media_type=media_type)

    def put_json(self, document: Any) -> BlobRef:
        raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return self.put(raw, "application/json")

    def get(self, digest: str) -> bytes:
        path = self._path_for(digest)
        if not path.exists():
            raise FileNotFoundError("no blob with digest " + digest)
        return path.read_bytes()

    def get_json(self, digest: str) -> Any:
        return json.loads(self.get(digest).decode("utf-8"))

    def exists(self, digest: str) -> bool:
        return self._path_for(digest).exists()

    def stats(self) -> Dict[str, Any]:
        count = 0
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file() and not path.name.endswith(".part"):
                count += 1
                total += path.stat().st_size
        return {"blobs": count, "bytes": total, "root": str(self.root)}


# -- parquet sinks ------------------------------------------------------------


class Lakehouse:
    """Hive-partitioned Parquet sinks plus a DuckDB reader, over one data root."""

    def __init__(self, data_dir: Path, writer_id: str = "unowned"):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.writer_id = writer_id
        self.blobs = BlobStore(self.root / BLOB_DIRNAME)
        self._lock = threading.RLock()
        self._buffers: Dict[str, List[Dict[str, Any]]] = {name: [] for name in DATASETS}

    # -- ownership ---------------------------------------------------------

    def claim_writer(self) -> Dict[str, Any]:
        """Record which process owns these partitions, and refuse a second one.

        Parquet has no locking. Two appenders to one partition directory produce
        interleaved partial files with nothing to arbitrate them, so ownership is
        asserted rather than assumed (V10-DAT-004).
        """
        marker = self.root / "_writer.json"
        with self._lock:
            if marker.exists():
                try:
                    held = json.loads(marker.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    held = {}
                owner = held.get("writerId")
                if owner and owner != self.writer_id:
                    raise LakehouseUnavailable(
                        "these Parquet partitions are owned by writer " + repr(owner)
                        + "; " + repr(self.writer_id) + " must not append to them. "
                        "Exactly one process writes a partition (V10-DAT-004)"
                    )
            record = {"writerId": self.writer_id, "claimedAt": _now()}
            marker.write_text(json.dumps(record, indent=2), encoding="utf-8")
            return record

    # -- writing -----------------------------------------------------------

    def _partition_dir(self, dataset: str, row: Mapping[str, Any]) -> Path:
        directory = self.root / dataset
        for column in DATASETS[dataset]["partition_by"]:
            value = str(row.get(column) or "unknown").replace("/", "_").replace("\\", "_")
            directory = directory / (column + "=" + value)
        return directory

    def append(self, dataset: str, row: Mapping[str, Any]) -> Path:
        """Append one row to its Hive partition as a JSONL shard.

        Rows land as newline-delimited JSON first and are compacted into Parquet
        by :meth:`compact`. Writing one Parquet file per row would be pathological
        -- Parquet's whole advantage is columnar batching -- and buffering only in
        memory would lose the row on restart, which for an audit trail is the one
        outcome that is not acceptable.
        """
        if dataset not in DATASETS:
            raise KeyError("unknown lakehouse dataset " + repr(dataset))
        enriched = dict(row)
        enriched.setdefault("event_month", _month(str(row.get("created_at") or "")))
        enriched.setdefault("ingested_at", _now())

        directory = self._partition_dir(dataset, enriched)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "pending.jsonl"
        with self._lock:
            with target.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(enriched, separators=(",", ":"), default=str) + "\n")
        return target

    def compact(self, dataset: Optional[str] = None) -> Dict[str, Any]:
        """Fold pending JSONL shards into Parquet. Requires DuckDB."""
        duckdb = duckdb_module()
        names = [dataset] if dataset else list(DATASETS)
        written: List[Dict[str, Any]] = []
        connection = duckdb.connect(database=":memory:")
        try:
            for name in names:
                for pending in sorted((self.root / name).rglob("pending.jsonl")):
                    directory = pending.parent
                    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
                    parquet = directory / ("part-" + stamp + "-" + os.urandom(3).hex() + ".parquet")
                    with self._lock:
                        # COPY ... TO does not accept a bound parameter for its
                        # destination, so the path is escaped inline while the
                        # source stays parameterised.
                        connection.execute(
                            "COPY (SELECT * FROM read_json_auto(?)) TO '"
                            + str(parquet).replace("'", "''")
                            + "' (FORMAT PARQUET, COMPRESSION ZSTD)",
                            [str(pending)],
                        )
                        rows = connection.execute(
                            "SELECT COUNT(*) FROM read_parquet(?)", [str(parquet)]
                        ).fetchone()[0]
                        pending.unlink()
                    written.append(
                        {"dataset": name, "file": str(parquet), "rows": int(rows)}
                    )
        finally:
            connection.close()
        self.write_manifest()
        return {"compacted": written, "at": _now()}

    # -- reading -----------------------------------------------------------

    def query(self, sql: str, parameters: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        """Run analytical SQL over the Parquet sinks. Refuses without DuckDB.

        Each dataset is exposed as a view. A sink with no files yet gets an empty
        view rather than no view at all: "no rows" is a real answer, and a
        missing table would look like a broken query instead.
        """
        duckdb = duckdb_module()
        connection = duckdb.connect(database=":memory:")
        try:
            for name in DATASETS:
                # A view unions compacted Parquet with the shards still pending
                # compaction. Reading only Parquet would mean an audit trail did
                # not show the transaction you just made, which for an audit
                # trail is the one freshness gap that is not acceptable.
                parts = []
                if list((self.root / name).rglob("*.parquet")):
                    glob = str(self.root / name / "**" / "*.parquet").replace("'", "''")
                    parts.append(
                        "SELECT * FROM read_parquet('" + glob
                        + "', hive_partitioning=true, union_by_name=true)"
                    )
                if list((self.root / name).rglob("pending.jsonl")):
                    glob = str(self.root / name / "**" / "pending.jsonl").replace("'", "''")
                    parts.append(
                        "SELECT * FROM read_json_auto('" + glob
                        + "', union_by_name=true, filename=false)"
                    )
                if parts:
                    body = " UNION ALL BY NAME ".join(parts)
                else:
                    body = "SELECT NULL AS placeholder WHERE FALSE"
                connection.execute("CREATE OR REPLACE VIEW " + name + " AS " + body)
            cursor = connection.execute(sql, list(parameters or []))
            columns = [d[0] for d in cursor.description] if cursor.description else []
            return [
                {k: _json_safe(v) for k, v in zip(columns, row)}
                for row in cursor.fetchall()
            ]
        finally:
            connection.close()

    # -- manifest ----------------------------------------------------------

    def write_manifest(self) -> Path:
        """Merge-only dataset manifest (Part 7.3).

        Derived, never authoritative: the files are the source of truth and this
        index is rebuildable from them, which is why `reindex` is a repair path
        and not a migration.
        """
        path = self.root / "manifest.json"
        existing: Dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}

        datasets = dict(existing.get("datasets") or {})
        for name, spec in DATASETS.items():
            files = sorted(str(p.relative_to(self.root)) for p in (self.root / name).rglob("*.parquet"))
            previous = datasets.get(name, {})
            datasets[name] = {
                **previous,
                "path": name + "/**/*.parquet",
                "partitions": list(spec["partition_by"]),
                "description": spec["description"],
                "files": len(files),
                "updatedAt": _now(),
            }

        document = {
            **existing,
            "version": MANIFEST_VERSION,
            "engine": "duckdb-parquet",
            "compression": "zstd",
            "writerId": self.writer_id,
            "blobStore": BLOB_DIRNAME,
            "datasets": datasets,
            "generatedAt": _now(),
        }
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path

    def reindex(self) -> Dict[str, Any]:
        """Rebuild the manifest from the files on disk."""
        path = self.write_manifest()
        return {"manifest": str(path), "at": _now()}

    # -- reporting ---------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        datasets = {}
        for name in DATASETS:
            base = self.root / name
            parquet = list(base.rglob("*.parquet"))
            pending = list(base.rglob("pending.jsonl"))
            datasets[name] = {
                "parquetFiles": len(parquet),
                "pendingShards": len(pending),
                "bytes": sum(p.stat().st_size for p in parquet),
                "partitionBy": list(DATASETS[name]["partition_by"]),
            }
        try:
            duckdb_module()
            engine = "available"
            engine_detail = ""
        except LakehouseUnavailable as exc:
            engine = "unavailable"
            engine_detail = str(exc)
        return {
            "root": str(self.root),
            "writerId": self.writer_id,
            "engine": engine,
            "engineDetail": engine_detail,
            "datasets": datasets,
            "blobs": self.blobs.stats(),
        }
