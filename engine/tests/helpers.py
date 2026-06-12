"""Synthetic JPEG / EXIF / bundle builders shared by intake tests."""
from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

from stockopoly.intake.manifest import MANIFEST_NAME, SCHEMA_VERSION, sha256_file

# ────────────────────────────────────────────────── TIFF / EXIF construction
_ASCII, _SHORT, _LONG, _RATIONAL = 2, 3, 4, 5


def _entry_bytes(tag: int, typ: int, values, heap: bytearray, heap_base: int) -> bytes:
    """One little-endian IFD entry; long values are appended to ``heap``."""
    if typ == _ASCII:
        raw = values.encode("ascii") + b"\x00"
        count = len(raw)
    elif typ == _RATIONAL:
        pairs = values if isinstance(values, list) else [values]
        raw = b"".join(struct.pack("<II", n, d) for n, d in pairs)
        count = len(pairs)
    elif typ == _SHORT:
        raw = struct.pack("<H", values)
        count = 1
    else:  # _LONG
        raw = struct.pack("<I", values)
        count = 1
    if len(raw) <= 4:
        value_field = raw.ljust(4, b"\x00")
    else:
        value_field = struct.pack("<I", heap_base + len(heap))
        heap.extend(raw)
    return struct.pack("<HHI", tag, typ, count) + value_field


def build_exif_tiff(*, model: str = "TestCam", datetime_original: str = "2026:06:01 10:00:00",
                    focal=(52, 10), focal_35: int = 26,
                    lat=(35, 30, 0.0), lat_ref: str = "N",
                    lng=(82, 15, 36.0), lng_ref: str = "W",
                    heading=(2925, 10)) -> bytes:
    """Little-endian TIFF with IFD0 → Exif IFD + GPS IFD, valid offsets."""
    def dms_rationals(dms):
        d, m, s = dms
        return [(int(d), 1), (int(m), 1), (int(round(s * 100)), 100)]

    ifd0_n, exif_n, gps_n = 3, 3, 5
    ifd0_off = 8
    exif_off = ifd0_off + 2 + ifd0_n * 12 + 4
    gps_off = exif_off + 2 + exif_n * 12 + 4
    heap_base = gps_off + 2 + gps_n * 12 + 4
    heap = bytearray()

    ifd0 = [
        _entry_bytes(0x0110, _ASCII, model, heap, heap_base),
        _entry_bytes(0x8769, _LONG, exif_off, heap, heap_base),
        _entry_bytes(0x8825, _LONG, gps_off, heap, heap_base),
    ]
    exif = [
        _entry_bytes(0x9003, _ASCII, datetime_original, heap, heap_base),
        _entry_bytes(0x920A, _RATIONAL, [focal], heap, heap_base),
        _entry_bytes(0xA405, _SHORT, focal_35, heap, heap_base),
    ]
    gps = [
        _entry_bytes(0x0001, _ASCII, lat_ref, heap, heap_base),
        _entry_bytes(0x0002, _RATIONAL, dms_rationals(lat), heap, heap_base),
        _entry_bytes(0x0003, _ASCII, lng_ref, heap, heap_base),
        _entry_bytes(0x0004, _RATIONAL, dms_rationals(lng), heap, heap_base),
        _entry_bytes(0x0011, _RATIONAL, [heading], heap, heap_base),
    ]

    out = bytearray()
    out += b"II" + struct.pack("<HI", 42, ifd0_off)
    for n, entries in ((ifd0_n, ifd0), (exif_n, exif), (gps_n, gps)):
        out += struct.pack("<H", n) + b"".join(entries) + struct.pack("<I", 0)
    out += heap
    return bytes(out)


def build_jpeg(width: int = 640, height: int = 480, *, exif: bytes | None = None,
               filler: bytes = b"") -> bytes:
    """Structurally valid JPEG: SOI, optional APP1 Exif, SOF0, SOS, EOI.

    Not decodable image data — enough for the marker walker (and stable
    sha256 fixity), which is all the stdlib intake path needs.
    """
    out = bytearray(b"\xff\xd8")
    if exif is not None:
        payload = b"Exif\x00\x00" + exif
        out += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    sof = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    out += b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    out += b"\xff\xda" + struct.pack(">H", 4) + b"\x01\x00"
    out += filler + b"\xff\xd9"
    return bytes(out)


# ───────────────────────────────────────────────────────── capture/1 bundles
def make_manifest(session_id: str, photo_files: list[Path], *,
                  preset: str = "orbit_object") -> dict:
    photos = []
    for i, p in enumerate(photo_files):
        photos.append({
            "file": f"photos/{p.name}",
            "index": i,
            "capturedAt": f"2026-06-01T10:0{i}:00.000Z",
            "pose": {"lat": 35.5 + i * 1e-5, "lng": -82.26, "accuracyM": 4.0,
                     "altM": 650.0, "headingDeg": 10.0 * i, "pitchDeg": -5.0,
                     "rollDeg": 0.5},
            "sectorIdx": i,
            "ring": "mid",
            "width": 640, "height": 480, "bytes": p.stat().st_size,
            "sha256": sha256_file(p),
            "quality": {"blurScore": 120.5, "blurry": False, "brightness": 128.0,
                        "badExposure": False},
        })
    return {
        "schema": SCHEMA_VERSION,
        "generatedAt": "2026-06-01T10:05:00.000Z",
        "session": {"id": session_id, "name": "Rack A scan", "preset": preset,
                    "startedAt": "2026-06-01T10:00:00.000Z",
                    "completedAt": "2026-06-01T10:05:00.000Z",
                    "operator": "tester", "notes": "",
                    "origin": {"lat": 35.5, "lng": -82.26, "accuracyM": 4.0},
                    "device": {"userAgent": "pytest", "platform": "linux"}},
        "project": {"id": "proj-1", "name": "Warehouse 7", "site": "Asheville",
                    "tags": ["test"]},
        "photos": photos,
        "coverage": {"sectors": 12, "filled": len(photos),
                     "pct": round(100.0 * len(photos) / 12, 1)},
        "intake": {"targetDataStore": "supply-chain-brain",
                   "intakeModule": "src.photogrammetry",
                   "kind": "photogrammetry_capture"},
    }


def make_bundle_zip(tmp_path: Path, session_id: str = "sess-0001",
                    n_photos: int = 2) -> Path:
    """Build a contract-shaped bundle ZIP with n synthetic JPEGs."""
    work = tmp_path / f"bundle_src_{session_id}"
    (work / "photos").mkdir(parents=True)
    files = []
    for i in range(n_photos):
        p = work / "photos" / f"IMG_{i:04d}.jpg"
        p.write_bytes(build_jpeg(filler=bytes([i]) * 32))
        files.append(p)
    manifest = make_manifest(session_id, files)
    (work / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    zip_path = tmp_path / f"bundle_{session_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(work / MANIFEST_NAME, MANIFEST_NAME)
        for p in files:
            zf.write(p, f"photos/{p.name}")
    return zip_path
