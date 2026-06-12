"""Vendored ``loadopoly.capture/1`` manifest reader (stdlib only).

This is StockOpoly's copy of the bundle contract consumer. The producer is
``../Loadopoly-OCR/src/capture/types.ts`` (`CAPTURE_SCHEMA_VERSION`,
`ManifestPhoto`, `CaptureManifest`) and the reference consumer is
``../Supply-Chain-Brain/pipeline/src/photogrammetry/__init__.py``. Field
names below must stay byte-compatible with both; ``tests/test_manifest.py``
pins the schema literal against the producer source when the sibling
checkout is present.

Manifest shape (loadopoly.capture/1)::

    schema       "loadopoly.capture/1"
    generatedAt  ISO timestamp
    session      {id, name, preset, startedAt, completedAt, operator, notes,
                  origin{lat,lng,accuracyM}|null, device{userAgent,platform}}
    project      {id, name, site, tags[]}
    photos[]     {file "photos/IMG_0001.jpg", index, capturedAt,
                  pose{lat,lng,accuracyM,altM,headingDeg,pitchDeg,rollDeg},
                  sectorIdx, ring, width, height, bytes, sha256,
                  quality{blurScore,blurry,brightness,badExposure?}}
    coverage     {sectors, filled, pct}
    intake       {targetDataStore, intakeModule, kind}
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "loadopoly.capture/1"
MANIFEST_NAME = "scb_manifest.json"


class BundleError(ValueError):
    """Raised when a bundle is structurally invalid."""


def load_manifest(bundle_dir: Path) -> dict[str, Any]:
    """Read + validate ``scb_manifest.json`` from an extracted bundle dir."""
    manifest_path = bundle_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise BundleError(f"{MANIFEST_NAME} not found in {bundle_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc

    if manifest.get("schema") != SCHEMA_VERSION:
        raise BundleError(
            f"Unsupported manifest schema {manifest.get('schema')!r} "
            f"(expected {SCHEMA_VERSION!r})"
        )
    for key in ("session", "project", "photos"):
        if key not in manifest:
            raise BundleError(f"Manifest missing required key: {key!r}")
    if not manifest["session"].get("id"):
        raise BundleError("Manifest session.id is empty")
    return manifest


def extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract a bundle ZIP, refusing entries that escape ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = (dest / member).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise BundleError(f"Unsafe path in bundle: {member}")
        zf.extractall(dest)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
