"""Photo intake — capture bundles and loose JPEG batches → ``stockopoly.sqlite``.

Two front doors, one destination (the ``batches`` + ``photos`` tables):

* ``ingest_bundle(path)`` — a ``loadopoly.capture/1`` ZIP or extracted folder
  from the Loadopoly-OCR Operate Console. Pose/quality come from the
  manifest; per-photo SHA-256 fixity is verified like the Brain's intake.
* ``ingest_loose(source)`` — any folder (or explicit list) of JPEGs, e.g.
  phone-camera uploads. Pose comes from EXIF GPS/heading; dimensions from
  the JPEG SOF marker; perceptual hashes when Pillow is available.

Staged files live under ``engine/data/batches/<batch_id>/`` (gitignored).
Every successful ingest records a ``stockopoly_intake`` event, which mirrors
into the Brain's learning_log via scb_link.
"""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import events
from ..store import data_dir, open_conn
from . import hashes
from .exif import parse_jpeg
from .manifest import (
    MANIFEST_NAME,
    SCHEMA_VERSION,
    BundleError,
    extract_zip,
    load_manifest,
    sha256_file,
)

__all__ = [
    "SCHEMA_VERSION", "MANIFEST_NAME", "BundleError",
    "ingest_bundle", "ingest_loose", "list_batches", "batch_photos",
    "batches_dir",
]

logger = logging.getLogger(__name__)

_JPEG_SUFFIXES = {".jpg", ".jpeg"}


def batches_dir() -> Path:
    return data_dir() / "batches"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _batch_exists(cn, batch_id: str) -> bool:
    return cn.execute(
        "SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)
    ).fetchone() is not None


def _replace_batch(cn, batch_id: str) -> None:
    cn.execute("DELETE FROM photos WHERE batch_id=?", (batch_id,))
    cn.execute("DELETE FROM batches WHERE batch_id=?", (batch_id,))


# ──────────────────────────────────────────────────────────── capture bundles
def ingest_bundle(path: str | Path, *, verify: bool = True,
                  force: bool = False) -> dict[str, Any]:
    """Stage + register one ``loadopoly.capture/1`` bundle (ZIP or directory).

    Returns a summary dict; raises ``BundleError`` on structural problems and
    ``FileExistsError`` when the session is already a batch (unless force).
    """
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise BundleError(f"Bundle not found: {src}")

    staged_root = batches_dir()
    if src.is_file():
        tmp_dir = staged_root / f"_incoming_{src.stem}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        extract_zip(src, tmp_dir)
        bundle_dir = tmp_dir
    else:
        bundle_dir = src

    try:
        manifest = load_manifest(bundle_dir)
    except BundleError:
        if src.is_file():
            shutil.rmtree(bundle_dir, ignore_errors=True)
        raise

    session = manifest["session"]
    photos: list[dict[str, Any]] = manifest.get("photos", [])
    batch_id: str = session["id"]
    staged = staged_root / batch_id

    cn = open_conn()
    try:
        if _batch_exists(cn, batch_id):
            if not force:
                if src.is_file():
                    shutil.rmtree(bundle_dir, ignore_errors=True)
                raise FileExistsError(
                    f"Batch {batch_id[:8]} already ingested (use force=True to replace)")
            _replace_batch(cn, batch_id)

        if bundle_dir != staged:
            if staged.exists():
                shutil.rmtree(staged)
            if src.is_file():
                bundle_dir.rename(staged)
            else:
                shutil.copytree(bundle_dir, staged)

        now = _now()
        cn.execute(
            "INSERT INTO batches(batch_id, kind, source_name, created_at,"
            " photo_count, status, manifest_json) VALUES (?,?,?,?,?,?,?)",
            (batch_id, "bundle", src.name, now, len(photos), "new",
             json.dumps(manifest, separators=(",", ":"))),
        )
        fixity_failures = 0
        staged_resolved = staged.resolve()
        for photo in photos:
            rel = photo.get("file", "")
            abs_path = (staged / rel).resolve()
            if not abs_path.is_relative_to(staged_resolved):
                # Manifest entry tries to escape the staged bundle dir — never
                # record such a path (it would be read back verbatim later).
                fixity_failures += 1
                logger.warning("Skipping photo escaping bundle dir: %s", rel)
                continue
            expected = (photo.get("sha256") or "").lower()
            if not abs_path.exists():
                fixity_failures += 1
                logger.warning("Missing photo in bundle: %s", rel)
            elif verify and expected and sha256_file(abs_path) != expected:
                fixity_failures += 1
                logger.warning("Fixity mismatch for %s", rel)
            pose = photo.get("pose") or {}
            quality = photo.get("quality") or {}
            cn.execute(
                "INSERT OR REPLACE INTO photos("
                "photo_id, batch_id, file, abs_path, sha256, captured_at,"
                "lat, lng, alt_m, heading_deg, pitch_deg, roll_deg,"
                "width, height, bytes, focal_mm, focal_35mm, camera_model,"
                "blur_score, dhash, ahash, exif_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{batch_id}::{rel}", batch_id, rel, str(abs_path),
                    photo.get("sha256"), photo.get("capturedAt"),
                    pose.get("lat"), pose.get("lng"), pose.get("altM"),
                    pose.get("headingDeg"), pose.get("pitchDeg"), pose.get("rollDeg"),
                    photo.get("width"), photo.get("height"), photo.get("bytes"),
                    None, None, None,
                    quality.get("blurScore"),
                    hashes.dhash(abs_path) if abs_path.exists() else None,
                    hashes.ahash(abs_path) if abs_path.exists() else None,
                    json.dumps({"ring": photo.get("ring"),
                                "sectorIdx": photo.get("sectorIdx"),
                                "quality": quality}, separators=(",", ":")),
                    now,
                ),
            )
        cn.commit()
    finally:
        cn.close()

    coverage_pct = float((manifest.get("coverage") or {}).get("pct") or 0.0)
    summary = {
        "batch_id": batch_id,
        "kind": "bundle",
        "session_name": session.get("name"),
        "preset": session.get("preset"),
        "site": (manifest.get("project") or {}).get("site"),
        "photo_count": len(photos),
        "coverage_pct": coverage_pct,
        "fixity_failures": fixity_failures,
        "staged_dir": str(staged),
    }
    events.record(
        "stockopoly_intake", summary,
        title=f"Bundle [{batch_id[:8]}] ×{len(photos)} → StockOpoly",
        signal=round(max(coverage_pct, 1.0) / 100.0, 4),
    )
    logger.info("Ingested capture bundle: %s", summary)
    return summary


# ─────────────────────────────────────────────────────────────── loose JPEGs
def _iter_jpegs(source: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(source, (str, Path)):
        root = Path(source).expanduser().resolve()
        if root.is_dir():
            return sorted(p for p in root.rglob("*")
                          if p.suffix.lower() in _JPEG_SUFFIXES and p.is_file())
        return [root] if root.suffix.lower() in _JPEG_SUFFIXES else []
    return [Path(p).expanduser().resolve() for p in source
            if Path(p).suffix.lower() in _JPEG_SUFFIXES]


def ingest_loose(source: str | Path | Iterable[str | Path], *,
                 name: str | None = None) -> dict[str, Any]:
    """Copy a folder/list of JPEGs into a new ``loose`` batch.

    EXIF supplies pose + capture time where present; files without metadata
    still ingest (grouping then relies on manual/visual signals).
    """
    files = _iter_jpegs(source)
    if not files:
        raise BundleError(f"No JPEG files found in {source}")

    batch_id = f"loose_{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
    source_name = name or (Path(source).name if isinstance(source, (str, Path))
                           else f"{len(files)} files")
    staged = batches_dir() / batch_id
    staged.mkdir(parents=True, exist_ok=True)

    now = _now()
    geotagged = 0
    cn = open_conn()
    try:
        cn.execute(
            "INSERT INTO batches(batch_id, kind, source_name, created_at,"
            " photo_count, status, manifest_json) VALUES (?,?,?,?,?,?,?)",
            (batch_id, "loose", source_name, now, len(files), "new", None),
        )
        used: set[str] = set()
        for f in files:
            rel = f.name
            stem, dot, ext = rel.rpartition(".")
            n = 1
            while rel in used:  # de-dupe colliding filenames across subfolders
                n += 1
                rel = f"{stem}_{n}{dot}{ext}" if dot else f"{rel}_{n}"
            used.add(rel)
            dest = staged / rel
            shutil.copy2(f, dest)

            meta = parse_jpeg(dest)
            if meta["lat"] is not None:
                geotagged += 1
            cn.execute(
                "INSERT OR REPLACE INTO photos("
                "photo_id, batch_id, file, abs_path, sha256, captured_at,"
                "lat, lng, alt_m, heading_deg, pitch_deg, roll_deg,"
                "width, height, bytes, focal_mm, focal_35mm, camera_model,"
                "blur_score, dhash, ahash, exif_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{batch_id}::{rel}", batch_id, rel, str(dest),
                    sha256_file(dest), meta["captured_at"],
                    meta["lat"], meta["lng"], meta["alt_m"],
                    meta["heading_deg"], None, None,
                    meta["width"], meta["height"], dest.stat().st_size,
                    meta["focal_mm"], meta["focal_35mm"], meta["camera_model"],
                    hashes.blur_score(dest), hashes.dhash(dest), hashes.ahash(dest),
                    json.dumps(meta, separators=(",", ":")), now,
                ),
            )
        cn.commit()
    finally:
        cn.close()

    summary = {
        "batch_id": batch_id,
        "kind": "loose",
        "source_name": source_name,
        "photo_count": len(files),
        "geotagged": geotagged,
        "staged_dir": str(staged),
    }
    events.record(
        "stockopoly_intake", summary,
        title=f"Loose batch [{source_name}] ×{len(files)} → StockOpoly",
        signal=0.4,
    )
    logger.info("Ingested loose batch: %s", summary)
    return summary


# ─────────────────────────────────────────────────────────────────── queries
def list_batches() -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        rows = cn.execute(
            "SELECT batch_id, kind, source_name, created_at, photo_count, status"
            " FROM batches ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        cn.close()


def batch_photos(batch_id: str) -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        rows = cn.execute(
            "SELECT photo_id, file, abs_path, sha256, captured_at, lat, lng,"
            " alt_m, heading_deg, pitch_deg, roll_deg, width, height, bytes,"
            " focal_mm, focal_35mm, camera_model, blur_score, dhash, ahash"
            " FROM photos WHERE batch_id=? ORDER BY file", (batch_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        cn.close()
