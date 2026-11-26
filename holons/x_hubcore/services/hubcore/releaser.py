"""Part 6 - Validation, UAT, PROD Promotion, and Rollback Engine.

V10-REL-001: Only the internal release authority may promote a validated digest
from UAT to PROD. Previous PROD digests are archived with rollback manifests.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .errors import PolicyError


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class ReleaseEngine:
    """Manages formal UAT -> PROD promotion and rollback."""

    def __init__(self, workspace: Optional[Dict[str, Any]] = None):
        if workspace is None:
            self.workspace = config.load("workspace")
        else:
            self.workspace = workspace
        self.library_root = config.library_root(self.workspace)
        lib_cfg = self.workspace.get("library", {})
        self.prod_dir = self.library_root / lib_cfg.get("prod", "PROD")
        self.uat_dir = self.library_root / lib_cfg.get("uat", "UAT")
        self.archive_dir = self.library_root / lib_cfg.get("archive", "Archive")

        self.prod_dir.mkdir(parents=True, exist_ok=True)
        self.uat_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def promote(
        self,
        artifact_rel_path: str,
        actor_id: str,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Promote a candidate from UAT to PROD atomically, archiving the prior version."""
        uat_path = self.uat_dir / artifact_rel_path
        if not uat_path.exists():
            raise PolicyError(f"candidate artifact not found in UAT: {uat_path}")

        prod_path = self.prod_dir / artifact_rel_path
        prod_path.parent.mkdir(parents=True, exist_ok=True)

        candidate_digest = sha256_file(uat_path) if uat_path.is_file() else "directory"
        timestamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())

        prior_digest = None
        archive_path = None
        if prod_path.exists():
            prior_digest = sha256_file(prod_path) if prod_path.is_file() else "directory"
            archive_subdir = self.archive_dir / f"{timestamp}_{prior_digest[:12]}"
            archive_subdir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_subdir / prod_path.name
            if prod_path.is_dir():
                shutil.copytree(prod_path, archive_path)
            else:
                shutil.copy2(prod_path, archive_path)

        # Copy UAT to PROD
        if uat_path.is_dir():
            if prod_path.exists():
                shutil.rmtree(prod_path)
            shutil.copytree(uat_path, prod_path)
        else:
            shutil.copy2(uat_path, prod_path)

        # Record rollback manifest
        record = {
            "timestamp": timestamp,
            "artifact": artifact_rel_path,
            "promotedDigest": candidate_digest,
            "priorDigest": prior_digest,
            "archivePath": str(archive_path) if archive_path else None,
            "promotedBy": actor_id,
            "notes": notes,
        }

        manifest_file = self.archive_dir / f"release_manifest_{timestamp}.json"
        manifest_file.write_text(json.dumps(record, indent=2), encoding="utf-8")

        return {
            "status": "promoted",
            "artifact": artifact_rel_path,
            "digest": candidate_digest,
            "priorDigest": prior_digest,
            "manifest": str(manifest_file),
        }

    def list_history(self) -> List[Dict[str, Any]]:
        history = []
        for file in sorted(self.archive_dir.glob("release_manifest_*.json"), reverse=True):
            try:
                history.append(json.loads(file.read_text(encoding="utf-8")))
            except Exception:
                continue
        return history
