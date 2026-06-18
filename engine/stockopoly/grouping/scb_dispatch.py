"""Tier-3 grouping (preferred route) — delegate vision to the Supply-Chain-Brain.

StockOpoly's engine is stdlib-first and deliberately does *not* own a model
registry, router or multi-provider caller — the Brain does. When the Brain
sibling is importable this module hands it the same photo-grouping request
``llm.py`` would otherwise POST straight to OpenRouter, but routes it through
``brain.llm_ensemble.llm_ensemble_call`` so the call gets the Brain's
capability-weighted model selection (``llm_router``) and its OpenRouter/xAI
caller with fallback chain — rather than StockOpoly's hardcoded single model.
This is the active analogue of tier 1 (``scb_vision``), which only reads what the
Brain already knows; here StockOpoly actively borrows the Brain's vision compute.

Everything degrades. If the Brain is absent, not importable (its heavier deps
missing), or returns an offline/error sentinel, :func:`propose` returns ``None``
and the cascade falls back to the standalone ``llm.py`` transport. The Brain is
imported lazily and the import is fully guarded, so StockOpoly's engine core
stays stdlib-only and import-safe when standalone.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable

from ..scb_link import find_scb_repo
from . import vision_common

logger = logging.getLogger(__name__)

# The Brain task profile to route grouping through — an existing vision-weighted
# profile in the Brain's config/brain.yaml. Unknown tasks fall back to the
# router's "default" profile inside the Brain, so this never hard-fails.
_TASK = "perception_visual"


def _brain_src_dir() -> Path | None:
    """``<scb>/pipeline/src`` (the import root for the ``brain`` package)."""
    repo = find_scb_repo()
    if repo is None:
        return None
    src = repo / "pipeline" / "src"
    return src if (src / "brain").is_dir() else None


def _load_ensemble_call() -> Callable[..., dict] | None:
    """Import the Brain's ensemble entrypoint, or ``None`` when unreachable."""
    src = _brain_src_dir()
    if src is None:
        return None
    p = str(src)
    if p not in sys.path:
        sys.path.append(p)  # append: never shadow StockOpoly/stdlib modules
    try:
        from brain.llm_ensemble import llm_ensemble_call  # type: ignore
    except Exception as exc:  # missing deps, config, import error → go standalone
        logger.debug("SCB ensemble import unavailable: %s", exc)
        return None
    return llm_ensemble_call


def available() -> bool:
    """True when the Brain's vision ensemble can be reached in-process."""
    return _load_ensemble_call() is not None


def _is_sentinel(text: str) -> bool:
    """Detect the Brain's soft-failure replies (caught error / no live keys)."""
    return (text.startswith("[llm_ensemble_call error]")
            or "offline — no provider keys" in text)


def propose(photos: list[dict], batch: dict, cfg: dict) -> list[dict[str, Any]] | None:
    """Delegate tier-3 grouping to the Brain.

    Returns a (possibly empty) proposal list when the Brain actually ran, or
    ``None`` when the Brain is unreachable / errored so :mod:`grouping` can fall
    back to the standalone OpenRouter transport.
    """
    call = _load_ensemble_call()
    if call is None:
        return None
    subset = photos[:vision_common.MAX_PHOTOS]
    content, sent_idx = vision_common.build_content(subset)
    if len(sent_idx) < 2:
        return []  # too few encodable images — the direct route can't do better

    messages = [{"role": "user", "content": content}]
    try:
        resp = call(messages, task=_TASK)
    except Exception as exc:  # llm_ensemble_call leaks select_llm/DDL errors
        logger.warning("SCB grouping dispatch failed: %s", exc)
        return None
    if not isinstance(resp, dict):
        return None
    text = resp.get("content") or ""
    if not text or _is_sentinel(text):
        return None
    parsed = vision_common.extract_json(text)
    if not parsed or not isinstance(parsed.get("groups"), list):
        return None
    return vision_common.parse_groups(
        parsed, subset, sent_idx,
        signal="llm_vision", route="scb_dispatch", model=resp.get("model"))
