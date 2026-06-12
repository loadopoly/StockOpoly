"""Tier-3 grouping — vision LLM via OpenRouter (Grok by default).

Strictly opt-in twice over: the ``llm_vision`` setting must be true AND
``OPENROUTER_API_KEY`` must be set. Photos are downscaled (Pillow) or sent
raw only when small; the model is asked for a JSON grouping of same-object
sets plus any visible scale references. Network/JSON failures degrade to an
empty proposal list — the cascade then stands on tiers 1–2.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
_MAX_PHOTOS = 24            # request size guard
_MAX_RAW_BYTES = 400_000    # only send un-downscaled files below this

_PROMPT = (
    "You see numbered warehouse photos. Group photos that show the SAME physical "
    "object/rack/area. Also flag photos containing an obvious scale reference "
    "(tape measure, ruler, standard pallet). Reply with ONLY JSON: "
    '{"groups":[{"label":str,"kind":"same_object"|"scale_reference",'
    '"indices":[int,...],"confidence":0..1}]}'
)


def _encode_photo(path: Path) -> str | None:
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


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def propose(photos: list[dict], batch: dict, cfg: dict) -> list[dict[str, Any]]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return []
    subset = photos[:_MAX_PHOTOS]
    content: list[dict[str, Any]] = [{"type": "text", "text": _PROMPT}]
    sent_idx: list[int] = []
    for i, p in enumerate(subset):
        uri = _encode_photo(Path(p["abs_path"]))
        if uri is None:
            continue
        sent_idx.append(i)
        content.append({"type": "text", "text": f"Photo {len(sent_idx) - 1}: {p['file']}"})
        content.append({"type": "image_url", "image_url": {"url": uri}})
    if len(sent_idx) < 2:
        return []

    body = json.dumps({
        "model": cfg.get("llm_vision_model", "x-ai/grok-2-vision-1212"),
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.1,
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "X-Title": "StockOpoly grouping"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = payload["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("LLM grouping call failed: %s", exc)
        return []

    parsed = _extract_json(text or "")
    if not parsed or not isinstance(parsed.get("groups"), list):
        return []

    proposals: list[dict[str, Any]] = []
    for g in parsed["groups"]:
        try:
            members = [subset[sent_idx[int(i)]]["photo_id"] for i in g["indices"]
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
            "meta": {"signal": "llm_vision",
                     "model": cfg.get("llm_vision_model")},
        })
    return proposals
