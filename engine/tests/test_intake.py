"""Bundle + loose intake end-to-end against a temp DB."""
from __future__ import annotations

import json
import zipfile

import pytest

from stockopoly import intake
from stockopoly.intake.exif import parse_jpeg
from stockopoly.store import open_conn
from tests.helpers import build_exif_tiff, build_jpeg, make_bundle_zip


def _rows(sql, *args):
    cn = open_conn()
    try:
        return cn.execute(sql, args).fetchall()
    finally:
        cn.close()


# ─────────────────────────────────────────────────────────────────── bundles
def test_ingest_bundle_zip(tmp_path):
    zip_path = make_bundle_zip(tmp_path, "sess-aaaa", n_photos=3)
    summary = intake.ingest_bundle(zip_path)

    assert summary["batch_id"] == "sess-aaaa"
    assert summary["photo_count"] == 3
    assert summary["fixity_failures"] == 0
    assert summary["coverage_pct"] == 25.0

    batches = _rows("SELECT * FROM batches")
    assert len(batches) == 1 and batches[0]["kind"] == "bundle"
    manifest = json.loads(batches[0]["manifest_json"])
    assert manifest["schema"] == intake.SCHEMA_VERSION

    photos = intake.batch_photos("sess-aaaa")
    assert len(photos) == 3
    p0 = photos[0]
    assert p0["photo_id"] == "sess-aaaa::photos/IMG_0000.jpg"
    assert p0["lat"] == pytest.approx(35.5)
    assert p0["heading_deg"] == 0.0
    assert p0["width"] == 640 and p0["bytes"] > 0
    assert len(p0["sha256"]) == 64
    # staged file exists where abs_path says
    from pathlib import Path
    assert Path(p0["abs_path"]).exists()


def test_ingest_bundle_duplicate_and_force(tmp_path):
    zip_path = make_bundle_zip(tmp_path, "sess-bbbb")
    intake.ingest_bundle(zip_path)
    zip2 = make_bundle_zip(tmp_path / "again", "sess-bbbb")
    with pytest.raises(FileExistsError):
        intake.ingest_bundle(zip2)
    summary = intake.ingest_bundle(zip2, force=True)
    assert summary["batch_id"] == "sess-bbbb"
    assert len(_rows("SELECT * FROM batches")) == 1
    assert len(_rows("SELECT * FROM photos")) == 2


def test_ingest_bundle_fixity_failure(tmp_path):
    zip_path = make_bundle_zip(tmp_path, "sess-cccc", n_photos=1)
    # Corrupt the photo inside the zip (keep manifest sha256 stale)
    from stockopoly.intake.manifest import extract_zip
    work = tmp_path / "tampered"
    extract_zip(zip_path, work)
    photo = work / "photos" / "IMG_0000.jpg"
    photo.write_bytes(build_jpeg(filler=b"tampered" * 8))
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(tampered, "w") as zf:
        for f in sorted(work.rglob("*")):
            if f.is_file():
                zf.write(f, str(f.relative_to(work)))

    summary = intake.ingest_bundle(tampered)
    assert summary["fixity_failures"] == 1
    # verify=False skips hashing
    summary2 = intake.ingest_bundle(tampered, verify=False, force=True)
    assert summary2["fixity_failures"] == 0


def test_ingest_records_learning_event(tmp_path):
    intake.ingest_bundle(make_bundle_zip(tmp_path, "sess-dddd"))
    evts = _rows("SELECT kind, payload_json FROM events")
    assert any(e["kind"] == "stockopoly_intake" for e in evts)
    # SCB sibling is absent under tests → event lands in scb_outbox
    pending = _rows("SELECT kind FROM scb_outbox WHERE flushed=0")
    assert any(p["kind"] == "stockopoly_intake" for p in pending)


# ──────────────────────────────────────────────────────────────── loose JPEGs
def test_exif_parser_roundtrip(tmp_path):
    jpg = tmp_path / "exif.jpg"
    jpg.write_bytes(build_jpeg(800, 600, exif=build_exif_tiff()))
    meta = parse_jpeg(jpg)
    assert (meta["width"], meta["height"]) == (800, 600)
    assert meta["captured_at"] == "2026-06-01T10:00:00"
    assert meta["camera_model"] == "TestCam"
    assert meta["focal_mm"] == 5.2
    assert meta["focal_35mm"] == 26.0
    assert meta["lat"] == pytest.approx(35.5)
    assert meta["lng"] == pytest.approx(-82.26)
    assert meta["heading_deg"] == 292.5


def test_exif_parser_survives_garbage(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xe1\x00\x08Exif\x00\x00garbage")
    meta = parse_jpeg(bad)
    assert meta["width"] is None and meta["lat"] is None
    assert parse_jpeg(tmp_path / "missing.jpg")["width"] is None


def test_ingest_loose_folder(tmp_path):
    src = tmp_path / "phone_dump"
    src.mkdir()
    (src / "a.jpg").write_bytes(build_jpeg(800, 600, exif=build_exif_tiff()))
    (src / "b.jpg").write_bytes(build_jpeg(640, 480))
    (src / "notes.txt").write_text("ignored")

    summary = intake.ingest_loose(src)
    assert summary["kind"] == "loose"
    assert summary["photo_count"] == 2
    assert summary["geotagged"] == 1

    photos = intake.batch_photos(summary["batch_id"])
    by_file = {p["file"]: p for p in photos}
    assert by_file["a.jpg"]["lat"] == pytest.approx(35.5)
    assert by_file["a.jpg"]["camera_model"] == "TestCam"
    assert by_file["a.jpg"]["captured_at"] == "2026-06-01T10:00:00"
    assert by_file["b.jpg"]["lat"] is None
    assert by_file["b.jpg"]["width"] == 640
    for p in photos:
        assert len(p["sha256"]) == 64

    assert any(b["batch_id"] == summary["batch_id"] for b in intake.list_batches())


def test_ingest_loose_empty_raises(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(intake.BundleError, match="No JPEG"):
        intake.ingest_loose(empty)
