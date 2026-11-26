"""Part 12.5 - tenant provisioning and allocation-space confinement.

Every request carries the canonical tenancy tuple:

    (orgId, siteId, deviceId, repoId, userId)

and every filesystem, SQLite, DuckDB, or vector operation resolves through
:func:`confine`, which canonicalizes the target and refuses anything that
escapes the tenant subtree.

V10-ALC-001: cross-tenant access fails closed at the storage engine.
V10-ALC-002: onboarding provisions the subtree, quota, and schema isolation
             before any endpoint or model may be invoked.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path, PurePath
from typing import Any, Dict, Iterable, Mapping, Optional

from .errors import PolicyError, TenancyViolation

#: Identifier grammar for every tenancy component. Deliberately narrow: these
#: values become path segments, so anything that could denote a parent, a device
#: path, an alternate data stream, or a UNC share is rejected outright.
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

#: Windows device names that must never become a directory (Part 2.2).
RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com" + str(n) for n in range(1, 10)]
    + ["lpt" + str(n) for n in range(1, 10)]
)

TENANT_SUBDIRS = ("datasets", "vectors", "blobs", "artifacts", "inbox", "outbox")


@dataclass(frozen=True)
class TenancyTuple:
    """The composite partition key (Part 12.5.1)."""

    org_id: str
    site_id: str
    device_id: str
    repo_id: str
    user_id: str

    def __post_init__(self) -> None:
        for field_name in ("org_id", "site_id", "device_id", "repo_id", "user_id"):
            validate_id(getattr(self, field_name), field_name)

    @property
    def allocation_space_id(self) -> str:
        return "alloc-" + self.org_id + "-" + self.repo_id

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)

    def partition_predicate(self) -> Dict[str, str]:
        """The WHERE-clause bindings a data engine must apply (Part 21.3)."""
        return {
            "org_id": self.org_id,
            "site_id": self.site_id,
            "device_id": self.device_id,
            "repo_id": self.repo_id,
        }


def validate_id(value: Any, label: str = "id") -> str:
    """Validate one tenancy identifier, raising :class:`PolicyError` if unsafe."""
    if not isinstance(value, str):
        raise PolicyError(label + " must be a string, got " + type(value).__name__)
    normalized = value.strip()
    if not ID_PATTERN.match(normalized):
        raise PolicyError(
            label + " " + repr(value) + " is not a permitted identifier; expected "
            "lowercase alphanumeric with . _ - and no path separators"
        )
    if normalized.split(".")[0] in RESERVED_NAMES:
        raise PolicyError(label + " " + repr(value) + " is a reserved device name")
    if normalized in (".", ".."):
        raise PolicyError(label + " " + repr(value) + " denotes a relative path")
    return normalized


def slugify_repo(repo: str) -> str:
    """Turn a repository reference such as ``loadopoly/VS-Code`` into an id.

    Repository references legitimately contain a slash; the allocation space
    flattens it so the value stays a single path segment.
    """
    slug = repo.strip().lower().replace("/", "-").replace("\\", "-")
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug).strip("-.")
    if not slug:
        raise PolicyError("repository reference " + repr(repo) + " has no usable characters")
    return validate_id(slug, "repo_id")


def _canonical(path: Path) -> Path:
    """Fully resolve a path, following symlinks and junctions."""
    return Path(os.path.realpath(str(path)))


def confine(root: Path | str, *parts: str) -> Path:
    """Resolve ``parts`` beneath ``root`` and refuse any escape.

    This is the single choke point named by Part 7.6 step 2 ("confine") and by
    the Part 12.5.4 Path Traversal Shield. It rejects absolute components, UNC
    and device paths, alternate data streams, ``..`` traversal, and symlink or
    junction escapes -- including a symlink created *after* the directory was
    provisioned, because resolution happens per call.
    """
    canonical_root = _canonical(Path(root))
    candidate = canonical_root
    for part in parts:
        if part in ("", "."):
            continue
        text = str(part)
        if ":" in text or text.startswith(("/", "\\")) or PurePath(text).is_absolute():
            raise TenancyViolation(
                "path component " + repr(text) + " is absolute, a device path, or "
                "an alternate data stream and is refused"
            )
        candidate = candidate / text
    resolved = _canonical(candidate)
    try:
        resolved.relative_to(canonical_root)
    except ValueError:
        raise TenancyViolation(
            "resolved path escapes the allocation root; access refused (V10-ALC-001)"
        ) from None
    return resolved


class AllocationEngine:
    """Provisions and confines tenant allocation spaces.

    ``library_root`` is the canonicalized library root from Part 2.2; every
    tenant path is derived from it and never from caller-supplied absolutes.
    """

    def __init__(self, library_root: Path | str, workspace: Optional[Mapping[str, Any]] = None):
        self.library_root = _canonical(Path(library_root))
        workspace = workspace or {}
        spaces = workspace.get("allocationSpaces", {}) if workspace else {}
        self.alloc_root_rel = str(spaces.get("root") or "Data/hub/allocations")
        self.parquet_root_rel = str(spaces.get("parquetRoot") or "Data/parquet/allocations")
        self.enforce_quotas = bool(spaces.get("enforceQuotas", True))
        self.default_quota_mb = int(spaces.get("defaultQuotaMb", 51200) or 51200)

    # -- path resolution ---------------------------------------------------

    @property
    def allocations_root(self) -> Path:
        root = self.library_root / self.alloc_root_rel
        root.mkdir(parents=True, exist_ok=True)
        return _canonical(root)

    @property
    def parquet_allocations_root(self) -> Path:
        root = self.library_root / self.parquet_root_rel
        root.mkdir(parents=True, exist_ok=True)
        return _canonical(root)

    def tenant_root(self, tenancy: TenancyTuple) -> Path:
        """The tenant subtree ``Data/hub/allocations/{orgId}/{repoId}/``."""
        return confine(self.allocations_root, tenancy.org_id, tenancy.repo_id)

    def org_root(self, tenancy: TenancyTuple) -> Path:
        return confine(self.allocations_root, tenancy.org_id)

    def user_root(self, tenancy: TenancyTuple) -> Path:
        return confine(self.allocations_root, tenancy.org_id, "users", tenancy.user_id)

    def parquet_partition(self, tenancy: TenancyTuple) -> Path:
        return confine(self.parquet_allocations_root, tenancy.org_id, tenancy.repo_id)

    def brain_db(self, tenancy: TenancyTuple) -> Path:
        return confine(self.tenant_root(tenancy), "brain.sqlite")

    def vectors_root(self, tenancy: TenancyTuple) -> Path:
        return confine(self.tenant_root(tenancy), "vectors")

    def resolve(self, tenancy: TenancyTuple, *relative: str) -> Path:
        """Resolve a tenant-relative path, failing closed on any escape."""
        return confine(self.tenant_root(tenancy), *relative)

    # -- provisioning ------------------------------------------------------

    def provision(self, tenancy: TenancyTuple, quota_mb: Optional[int] = None) -> Dict[str, Any]:
        """Idempotently create the tenant subtree, seed SQLite WAL, write policy.

        Re-running provision on an existing space preserves ``createdAt`` and the
        allocation id (V10-CFG-002).
        """
        tenant = self.tenant_root(tenancy)
        for name in TENANT_SUBDIRS:
            confine(tenant, name).mkdir(parents=True, exist_ok=True)
        self.user_root(tenancy).mkdir(parents=True, exist_ok=True)
        self.parquet_partition(tenancy).mkdir(parents=True, exist_ok=True)

        self._seed_brain_db(self.brain_db(tenancy), tenancy)

        org_config = confine(self.org_root(tenancy), "config.json")
        existing: Dict[str, Any] = {}
        if org_config.exists():
            try:
                existing = json.loads(org_config.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}

        record = {
            "schemaVersion": 10,
            "orgId": tenancy.org_id,
            "createdAt": existing.get("createdAt") or _now(),
            "updatedAt": _now(),
            "quotaMb": int(quota_mb or existing.get("quotaMb") or self.default_quota_mb),
            "enforceQuotas": self.enforce_quotas,
            "isolationMode": "strict-subpath-confinement",
            "repos": sorted(set(existing.get("repos", [])) | {tenancy.repo_id}),
        }
        org_config.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

        return {
            "allocationSpaceId": tenancy.allocation_space_id,
            "tenancy": tenancy.to_dict(),
            "tenantRoot": str(tenant),
            "brainDb": str(self.brain_db(tenancy)),
            "parquetPartition": str(self.parquet_partition(tenancy)),
            "vectors": str(self.vectors_root(tenancy)),
            "quotaMb": record["quotaMb"],
        }

    @staticmethod
    def _seed_brain_db(path: Path, tenancy: TenancyTuple) -> None:
        """Create the tenant SQLite store in WAL mode (Part 7.7)."""
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tenancy ("
                " key TEXT PRIMARY KEY,"
                " value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS audit_log ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " at TEXT NOT NULL,"
                " actor TEXT NOT NULL,"
                " action TEXT NOT NULL,"
                " subject TEXT,"
                " detail TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS task ("
                " task_id TEXT PRIMARY KEY,"
                " kind TEXT NOT NULL,"
                " state TEXT NOT NULL,"
                " aggregate_version INTEGER NOT NULL DEFAULT 0,"
                " org_id TEXT NOT NULL,"
                " repo_id TEXT NOT NULL,"
                " created_at TEXT NOT NULL,"
                " payload TEXT)"
            )
            for key, value in tenancy.to_dict().items():
                connection.execute(
                    "INSERT INTO tenancy(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            connection.commit()
        finally:
            connection.close()

    # -- enforcement -------------------------------------------------------

    def usage_mb(self, tenancy: TenancyTuple) -> float:
        total = 0
        for base in (self.tenant_root(tenancy), self.parquet_partition(tenancy)):
            for path in base.rglob("*"):
                if path.is_file():
                    try:
                        total += path.stat().st_size
                    except OSError:
                        continue
        return round(total / (1024 * 1024), 3)

    def assert_within_quota(self, tenancy: TenancyTuple, quota_mb: Optional[int] = None) -> float:
        used = self.usage_mb(tenancy)
        if not self.enforce_quotas:
            return used
        limit = self.default_quota_mb if quota_mb is None else int(quota_mb)
        if used > limit:
            raise PolicyError(
                "allocation space " + tenancy.allocation_space_id + " is over quota ("
                + str(used) + " MB used of " + str(limit) + " MB)"
            )
        return used

    def assert_same_tenant(self, tenancy: TenancyTuple, path: Path | str) -> Path:
        """Verify an already-resolved path belongs to this tenant.

        Used by the data plane before handing a SQLite handle or DuckDB cursor to
        a query, so a path that arrived from elsewhere still fails closed.
        """
        resolved = _canonical(Path(path))
        tenant = self.tenant_root(tenancy)
        parquet = self.parquet_partition(tenancy)
        for root in (tenant, parquet):
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        raise TenancyViolation(
            "path is outside allocation space " + tenancy.allocation_space_id
            + "; cross-tenant access refused (V10-ALC-001)"
        )

    def list_tenants(self) -> Iterable[Dict[str, str]]:
        root = self.allocations_root
        for org_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for repo_dir in sorted(p for p in org_dir.iterdir() if p.is_dir()):
                if repo_dir.name == "users":
                    continue
                yield {"orgId": org_dir.name, "repoId": repo_dir.name}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
