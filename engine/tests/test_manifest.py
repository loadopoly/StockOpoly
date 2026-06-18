"""Contract pins for the vendored loadopoly.capture/1 reader."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

from stockopoly.intake.manifest import (
    MANIFEST_NAME,
    SCHEMA_VERSION,
    BundleError,
    extract_zip,
    load_manifest,
)
from tests.helpers import build_jpeg, make_bundle_zip, make_manifest

_REPO_ROOT = Path(__file__).resolve().parents[2]  # StockOpoly/


def test_schema_literal_pinned():
    assert SCHEMA_VERSION == "loadopoly.capture/1"
    assert MANIFEST_NAME == "scb_manifest.json"


def test_schema_matches_producer_types_ts():
    """When the Loadopoly-OCR sibling checkout exists, our literal must match
    its CAPTURE_SCHEMA_VERSION exactly (lockstep contract rule)."""
    types_ts = _REPO_ROOT.parent / "Loadopoly-OCR" / "src" / "capture" / "types.ts"
    if not types_ts.exists():
        pytest.skip("Loadopoly-OCR sibling checkout not present")
    m = re.search(r"CAPTURE_SCHEMA_VERSION\s*=\s*['\"]([^'\"]+)['\"]",
                  types_ts.read_text(encoding="utf-8"))
    assert m, "CAPTURE_SCHEMA_VERSION not found in producer types.ts"
    assert m.group(1) == SCHEMA_VERSION


def _brain_consumer_path():
    for name in ("VS Code", "VS-Code", "Supply-Chain-Brain"):
        candidate = (
            _REPO_ROOT.parent / name / "pipeline" / "src" / "photogrammetry" / "__init__.py"
        )
        if candidate.exists():
            return candidate
    return None


def test_schema_matches_brain_consumer():
    consumer = _brain_consumer_path()
    if consumer is None:
        pytest.skip("VS-Code / Supply-Chain-Brain sibling checkout not present")
    m = re.search(r"SCHEMA_VERSION\s*=\s*['\"]([^'\"]+)['\"]",
                  consumer.read_text(encoding="utf-8"))
    assert m and m.group(1) == SCHEMA_VERSION


def test_load_manifest_valid(tmp_path):
    photo = tmp_path / "photos" / "IMG_0000.jpg"
    photo.parent.mkdir()
    photo.write_bytes(build_jpeg())
    manifest = make_manifest("s1", [photo])
    (tmp_path / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_manifest(tmp_path)
    assert loaded["session"]["id"] == "s1"
    assert loaded["photos"][0]["file"] == "photos/IMG_0000.jpg"
    assert loaded["photos"][0]["pose"]["headingDeg"] == 0.0
    assert loaded["intake"]["kind"] == "photogrammetry_capture"


@pytest.mark.parametrize("mutate, msg", [
    (lambda m: m.update(schema="loadopoly.capture/2"), "Unsupported manifest schema"),
    (lambda m: m.pop("photos"), "missing required key"),
    (lambda m: m["session"].update(id=""), "session.id is empty"),
])
def test_load_manifest_rejects(tmp_path, mutate, msg):
    manifest = make_manifest("s1", [])
    mutate(manifest)
    (tmp_path / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match=msg):
        load_manifest(tmp_path)


def test_load_manifest_missing_file(tmp_path):
    with pytest.raises(BundleError, match="not found"):
        load_manifest(tmp_path)


def test_extract_zip_blocks_path_escape(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "nope")
    with pytest.raises(BundleError, match="Unsafe path"):
        extract_zip(evil, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_bundle_zip_helper_roundtrip(tmp_path):
    zip_path = make_bundle_zip(tmp_path, "sess-rt", n_photos=2)
    out = tmp_path / "extracted"
    extract_zip(zip_path, out)
    loaded = load_manifest(out)
    assert len(loaded["photos"]) == 2
    for p in loaded["photos"]:
        assert (out / p["file"]).exists()
