"""Minimal stdlib JPEG/EXIF reader for loose-photo intake.

Walks JPEG segments for dimensions (SOF0/1/2) and parses the APP1 Exif TIFF
directory for the handful of tags StockOpoly cares about: capture time,
camera make/model, focal length (real + 35mm-equivalent, used by the
dimension solver's distance estimates), GPS position/altitude and compass
heading. Anything unexpected is skipped — a malformed file yields whatever
fields were readable, never an exception.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                0xCD, 0xCE, 0xCF}

# tag → (name, ifd) for the tags we extract
_IFD0_TAGS = {0x010F: "make", 0x0110: "model", 0x0112: "orientation"}
_EXIF_TAGS = {0x9003: "datetime_original", 0x920A: "focal_mm",
              0xA405: "focal_35mm", 0xA002: "pixel_x", 0xA003: "pixel_y"}
_GPS_TAGS = {0x0001: "lat_ref", 0x0002: "lat_dms", 0x0003: "lng_ref",
             0x0004: "lng_dms", 0x0005: "alt_ref", 0x0006: "alt",
             0x0011: "img_direction"}

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _read_values(data: bytes, offset: int, typ: int, count: int, endian: str) -> Any:
    size = _TYPE_SIZES.get(typ)
    if size is None:
        return None
    total = size * count
    raw_off = offset + 8
    if total <= 4:
        raw = data[raw_off: raw_off + total]
    else:
        ptr = struct.unpack(endian + "I", data[raw_off: raw_off + 4])[0]
        raw = data[ptr: ptr + total]
    if len(raw) < total:
        return None
    if typ == 2:  # ASCII
        return raw.split(b"\x00")[0].decode("ascii", "replace").strip()
    fmt = {1: "B", 3: "H", 4: "I", 7: "B", 9: "i"}.get(typ)
    if fmt:
        vals = list(struct.unpack(endian + fmt * count, raw))
    elif typ in (5, 10):  # (S)RATIONAL pairs
        f = "I" if typ == 5 else "i"
        nums = struct.unpack(endian + f * (count * 2), raw)
        vals = [(nums[i] / nums[i + 1]) if nums[i + 1] else 0.0
                for i in range(0, count * 2, 2)]
    else:
        return None
    return vals[0] if count == 1 else vals


def _parse_ifd(data: bytes, offset: int, endian: str, tags: dict[int, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if offset + 2 > len(data):
        return out
    (n,) = struct.unpack(endian + "H", data[offset: offset + 2])
    for i in range(n):
        entry = offset + 2 + i * 12
        if entry + 12 > len(data):
            break
        tag, typ, count = struct.unpack(endian + "HHI", data[entry: entry + 8])
        if tag in tags:
            val = _read_values(data, entry, typ, count, endian)
            if val is not None:
                out[tags[tag]] = val
        elif tag in (0x8769, 0x8825):  # Exif / GPS IFD pointers
            ptr = _read_values(data, entry, typ, count, endian)
            if isinstance(ptr, int):
                sub = _EXIF_TAGS if tag == 0x8769 else _GPS_TAGS
                out.update(_parse_ifd(data, ptr, endian, sub))
    return out


def _parse_tiff(tiff: bytes) -> dict[str, Any]:
    if len(tiff) < 8:
        return {}
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        return {}
    (ifd0,) = struct.unpack(endian + "I", tiff[4:8])
    return _parse_ifd(tiff, ifd0, endian, _IFD0_TAGS)


def _dms_to_deg(dms: Any, ref: str | None) -> float | None:
    try:
        d, m, s = (list(dms) + [0.0, 0.0])[:3] if isinstance(dms, list) else (dms, 0.0, 0.0)
        deg = float(d) + float(m) / 60.0 + float(s) / 3600.0
    except (TypeError, ValueError):
        return None
    if ref in ("S", "W"):
        deg = -deg
    return round(deg, 7)


def _exif_datetime_iso(value: str | None) -> str | None:
    # EXIF format "YYYY:MM:DD HH:MM:SS" → ISO-8601 (naive; no zone in EXIF)
    if not value or len(value) < 19:
        return None
    date, _, time = value.partition(" ")
    return f"{date.replace(':', '-')}T{time}" if time else None


def parse_jpeg(path: str | Path) -> dict[str, Any]:
    """Best-effort metadata: dimensions + selected EXIF fields, never raises."""
    out: dict[str, Any] = {
        "width": None, "height": None, "captured_at": None,
        "camera_make": None, "camera_model": None,
        "focal_mm": None, "focal_35mm": None, "orientation": None,
        "lat": None, "lng": None, "alt_m": None, "heading_deg": None,
    }
    try:
        data = Path(path).read_bytes()
    except OSError:
        return out
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return out

    raw: dict[str, Any] = {}
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xD9 or marker == 0xDA:  # EOI / SOS — entropy data follows
            break
        (seg_len,) = struct.unpack(">H", data[pos + 2: pos + 4])
        seg = data[pos + 4: pos + 2 + seg_len]
        if marker == 0xE1 and seg[:6] == b"Exif\x00\x00":
            raw.update(_parse_tiff(seg[6:]))
        elif marker in _SOF_MARKERS and len(seg) >= 5:
            out["height"], out["width"] = struct.unpack(">HH", seg[1:5])
        pos += 2 + seg_len

    out["camera_make"] = raw.get("make") or None
    out["camera_model"] = raw.get("model") or None
    out["orientation"] = raw.get("orientation")
    out["captured_at"] = _exif_datetime_iso(raw.get("datetime_original"))
    if isinstance(raw.get("focal_mm"), (int, float)):
        out["focal_mm"] = round(float(raw["focal_mm"]), 3)
    if isinstance(raw.get("focal_35mm"), (int, float)):
        out["focal_35mm"] = float(raw["focal_35mm"])
    out["lat"] = _dms_to_deg(raw.get("lat_dms"), raw.get("lat_ref"))
    out["lng"] = _dms_to_deg(raw.get("lng_dms"), raw.get("lng_ref"))
    if isinstance(raw.get("alt"), (int, float)):
        alt = float(raw["alt"])
        if raw.get("alt_ref") == 1:
            alt = -alt
        out["alt_m"] = round(alt, 2)
    if isinstance(raw.get("img_direction"), (int, float)):
        out["heading_deg"] = round(float(raw["img_direction"]), 2)
    # Fall back to EXIF pixel dimensions when no SOF was found
    if out["width"] is None and isinstance(raw.get("pixel_x"), int):
        out["width"] = raw["pixel_x"]
    if out["height"] is None and isinstance(raw.get("pixel_y"), int):
        out["height"] = raw["pixel_y"]
    return out
