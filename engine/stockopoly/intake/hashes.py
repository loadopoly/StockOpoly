"""Perceptual hashes + blur scoring — Pillow-accelerated, absent-safe.

Pillow is an *optional* dependency (requirements-optional.txt). Without it
every function returns ``None`` and intake simply stores NULLs; grouping
tier 2 then leans on EXIF time/GPS instead of visual similarity.
"""
from __future__ import annotations

from pathlib import Path

try:
    from PIL import Image  # type: ignore
    HAVE_PIL = True
except ImportError:  # pragma: no cover - environment dependent
    Image = None  # type: ignore
    HAVE_PIL = False


def _gray_pixels(path: str | Path, w: int, h: int) -> list[int] | None:
    if not HAVE_PIL:
        return None
    try:
        with Image.open(path) as im:
            return list(im.convert("L").resize((w, h)).getdata())
    except Exception:
        return None


def dhash(path: str | Path) -> str | None:
    """64-bit difference hash (9×8 grayscale, row-wise gradient) as hex."""
    px = _gray_pixels(path, 9, 8)
    if px is None:
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] < px[row * 9 + col + 1] else 0)
    return f"{bits:016x}"


def ahash(path: str | Path) -> str | None:
    """64-bit average hash (8×8 grayscale vs mean) as hex."""
    px = _gray_pixels(path, 8, 8)
    if px is None:
        return None
    mean = sum(px) / 64.0
    bits = 0
    for v in px:
        bits = (bits << 1) | (1 if v >= mean else 0)
    return f"{bits:016x}"


def blur_score(path: str | Path) -> float | None:
    """Variance of a 3×3 Laplacian on a 128px-wide grayscale (higher = sharper).

    Comparable in spirit to the capture app's blurScore; only the relative
    ordering within a batch matters to grouping/QC.
    """
    if not HAVE_PIL:
        return None
    try:
        with Image.open(path) as im:
            g = im.convert("L")
            w, h = g.size
            if w > 128:
                h = max(1, round(h * 128 / w))
                w = 128
                g = g.resize((w, h))
            px = list(g.getdata())
    except Exception:
        return None
    if w < 3 or h < 3:
        return None
    vals: list[float] = []
    for y in range(1, h - 1):
        base = y * w
        for x in range(1, w - 1):
            i = base + x
            lap = (px[i - w] + px[i + w] + px[i - 1] + px[i + 1] - 4 * px[i])
            vals.append(float(lap))
    n = len(vals)
    mean = sum(vals) / n
    return round(sum((v - mean) ** 2 for v in vals) / n, 2)


def hamming(hex_a: str | None, hex_b: str | None) -> int | None:
    """Bit distance between two 64-bit hex hashes (None when either missing)."""
    if not hex_a or not hex_b:
        return None
    try:
        return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")
    except ValueError:
        return None
