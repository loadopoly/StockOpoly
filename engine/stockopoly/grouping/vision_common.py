"""Shared vision-grouping primitives for the tier-3 transports.

Both tier-3 routes — the standalone OpenRouter call (``llm.py``) and the
delegation to the Supply-Chain-Brain ensemble (``scb_dispatch.py``) — speak the
same OpenAI-style multimodal request and parse the same JSON grouping reply.
Keeping the prompt, image encoding, JSON extraction and proposal shaping in one
place means the two transports cannot drift apart; only *where the request is
sent* differs. Stdlib only; Pillow is an optional downscaling accelerator.
"""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any

MAX_PHOTOS = 24             # request-size guard (images per dispatch)
_MAX_RAW_BYTES = 400_000    # only send an un-downscaled file below this

PROMPT = (
    "You see numbered warehouse photos. Group photos that show the SAME physical "
    "object/rack/area. Also flag photos containing an obvious scale reference "
    "(tape measure, ruler, standard pallet). Reply with ONLY JSON: "
    '{"groups":[{"label":str,"kind":"same_object"|"scale_reference",'
    '"indices":[int,...],"confidence":0..1}]}'
)


def encode_photo(path: Path) -> str | None:
    """Return a data-URI JPEG, downscaled to ≤768px when Pillow is present."""
    try:
        try:
            from PIL import Image  # type: ignore
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((768, 768))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=70)
                raw = buf.getvalue()
        except ImportError:
            raw = path.read_bytes()
            if len(raw) > _MAX_RAW_BYTES:
                return None
    except Exception:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def build_content(photos: list[dict]) -> tuple[list[dict[str, Any]], list[int]]:
    """Build the OpenAI multimodal ``content`` array + the indices actually sent.

    Photos that fail to encode are skipped; the returned ``sent_idx`` maps each
    delivered image (in order) back to its position in ``photos`` so the model's
    0-based indices can be resolved to photo_ids by :func:`parse_groups`.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": PROMPT}]
    sent_idx: list[int] = []
    for i, p in enumerate(photos):
        uri = encode_photo(Path(p["abs_path"]))
        if uri is None:
            continue
        sent_idx.append(i)
        content.append({"type": "text", "text": f"Photo {len(sent_idx) - 1}: {p['file']}"})
        content.append({"type": "image_url", "image_url": {"url": uri}})
    return content, sent_idx


def extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def parse_groups(parsed: dict, photos: list[dict], sent_idx: list[int], *,
                 signal: str, route: str, model: str | None = None
                 ) -> list[dict[str, Any]]:
    """Turn a model's ``{"groups":[...]}`` reply into cascade proposals.

    ``photos`` is the same subset passed to :func:`build_content`; ``sent_idx``
    resolves the model's 0-based image indices back to photo_ids. ``route``
    records which transport produced the proposal (``openrouter_direct`` vs
    ``scb_dispatch``) so provenance survives into the group's stored meta.
    """
    proposals: list[dict[str, Any]] = []
    for g in parsed.get("groups") or []:
        try:
            members = [photos[sent_idx[int(i)]]["photo_id"] for i in g["indices"]
                       if 0 <= int(i) < len(sent_idx)]
        except (KeyError, TypeError, ValueError):
            continue
        kind = g.get("kind") if g.get("kind") in ("same_object", "scale_reference") \
            else "same_object"
        if not members or (kind == "same_object" and len(members) < 2):
            continue
        try:
            conf = max(0.0, min(float(g.get("confidence", 0.6)), 0.99))
        except (TypeError, ValueError):
            conf = 0.6
        proposals.append({
            "kind": kind,
            "label": str(g.get("label") or "LLM group")[:120],
            "confidence": conf,
            "photo_ids": sorted(set(members)),
            "meta": {"signal": signal, "route": route, "model": model},
        })
    return proposals
