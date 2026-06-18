"""Tier-3 grouping (standalone route) — vision LLM via OpenRouter (Grok default).

This is the *fallback* transport for tier 3: a direct OpenRouter call that needs
nothing but ``OPENROUTER_API_KEY``. When the Supply-Chain-Brain sibling is
reachable the cascade prefers ``scb_dispatch`` (which routes the same request
through the Brain's model registry + ensemble); this module is what runs when
StockOpoly is standalone. Strictly opt-in twice over: the ``llm_vision`` setting
must be true AND ``OPENROUTER_API_KEY`` must be set. The shared prompt, image
encoding and reply parsing live in ``vision_common`` so this route and the Brain
route stay byte-identical except for transport. Network/JSON failures degrade to
an empty proposal list — the cascade then stands on tiers 1–2.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from . import vision_common

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"


def propose(photos: list[dict], batch: dict, cfg: dict) -> list[dict[str, Any]]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return []
    subset = photos[:vision_common.MAX_PHOTOS]
    content, sent_idx = vision_common.build_content(subset)
    if len(sent_idx) < 2:
        return []

    model = cfg.get("llm_vision_model", "x-ai/grok-2-vision-1212")
    body = json.dumps({
        "model": model,
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

    parsed = vision_common.extract_json(text or "")
    if not parsed or not isinstance(parsed.get("groups"), list):
        return []
    return vision_common.parse_groups(
        parsed, subset, sent_idx,
        signal="llm_vision", route="openrouter_direct", model=model)
