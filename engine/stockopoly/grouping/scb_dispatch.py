"""Tier-3 grouping (preferred route) — delegate vision to the Supply-Chain-Brain.

StockOpoly's engine is stdlib-first and deliberately does *not* own a model
registry, router or multi-provider caller — the Brain does. When the Brain
sibling is importable this module hands it the same photo-grouping request
``llm.py`` would otherwise POST straight to OpenRouter, but routes it through
``brain.llm_ensemble.llm_ensemble_call`` so the call gets the Brain's
capability-weighted model selection (``llm_router``) and its OpenRouter/xAI
caller with fallback chain — rather than StockOpoly's hardcoded single model.

Cheap before costed: before spending any tokens, this consults the Brain's
``vlm_cache`` for a prior grouping of the *same photo set* (keyed by the sorted
photo sha256s, so it is reusable even across re-ingests). A hit is served for
zero tokens; a miss dispatches through the ensemble and writes the fresh result
back, so the next identical batch is free. This is the active analogue of tier 1
(``scb_vision``), which only *reads* what the Brain already knows.

Everything degrades. If the Brain is absent, not importable (heavier deps
missing), or returns an offline/error sentinel, :func:`propose` returns ``None``
and the cascade falls back to the standalone ``llm.py`` transport. The Brain is
imported lazily and fully guarded, so the engine core stays stdlib-only.
"""
from __future__ import annotations

import hashlib
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
_CACHE_KIND = "stockopoly_grouping"   # vlm_cache namespace for grouping results


def _brain_src_dir() -> Path | None:
    """``<scb>/pipeline/src`` (the import root for the ``brain`` package)."""
    repo = find_scb_repo()
    if repo is None:
        return None
    src = repo / "pipeline" / "src"
    return src if (src / "brain").is_dir() else None


def _import_brain(symbol: str):
    """Import ``brain.<symbol>`` lazily, or return ``None`` when unreachable."""
    src = _brain_src_dir()
    if src is None:
        return None
    p = str(src)
    if p not in sys.path:
        sys.path.append(p)  # append: never shadow StockOpoly/stdlib modules
    try:
        module = __import__(f"brain.{symbol}", fromlist=[symbol])
    except Exception as exc:  # missing deps, config, import error → go standalone
        logger.debug("SCB brain.%s import unavailable: %s", symbol, exc)
        return None
    return module


def _load_ensemble_call() -> Callable[..., dict] | None:
    """Import the Brain's ensemble entrypoint, or ``None`` when unreachable."""
    module = _import_brain("llm_ensemble")
    return getattr(module, "llm_ensemble_call", None) if module else None


def _load_vlm_cache():
    """Import the Brain's recall cache module, or ``None`` when unreachable."""
    return _import_brain("vlm_cache")


def available() -> bool:
    """True when the Brain's vision ensemble can be reached in-process."""
    return _load_ensemble_call() is not None


def _is_sentinel(text: str) -> bool:
    """Detect the Brain's soft-failure replies (caught error / no live keys)."""
    return (text.startswith("[llm_ensemble_call error]")
            or "offline — no provider keys" in text)


# ─────────────────────────────────────────────────────────── recall cache
def _content_key(subset: list[dict]) -> str | None:
    """Stable key over the photo *set* (sorted sha256s); None if any sha absent."""
    shas = [p.get("sha256") for p in subset]
    if not shas or not all(shas):
        return None
    payload = "stockopoly_grouping/v1|" + "|".join(sorted(shas))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recall_groups(cache, key: str, subset: list[dict]) -> list[dict] | None:
    """Return cached proposals remapped onto this batch, or None on a miss.

    A hit with an empty group list returns ``[]`` (cached "nothing to add") so
    the caller does not re-dispatch.
    """
    try:
        res = cache.recall(key, kind=_CACHE_KIND)
    except Exception as exc:
        logger.debug("SCB grouping recall failed: %s", exc)
        return None
    if not res:
        return None
    id_by_sha = {p["sha256"]: p["photo_id"] for p in subset if p.get("sha256")}
    out: list[dict] = []
    for g in (res.get("result") or {}).get("groups") or []:
        pids = sorted({id_by_sha[s] for s in (g.get("shas") or []) if s in id_by_sha})
        kind = g.get("kind") if g.get("kind") in ("same_object", "scale_reference") \
            else "same_object"
        if not pids or (kind == "same_object" and len(pids) < 2):
            continue
        meta = dict(g.get("meta") or {})
        meta.update({"route": "scb_cache", "cached": True})
        try:
            conf = max(0.0, min(float(g.get("confidence", 0.6)), 0.99))
        except (TypeError, ValueError):
            conf = 0.6
        out.append({"kind": kind, "label": str(g.get("label") or "LLM group")[:120],
                    "confidence": conf, "photo_ids": pids, "meta": meta})
    return out


def _remember_groups(cache, key: str, subset: list[dict], proposals: list[dict],
                     model: str | None) -> None:
    """Persist proposals as sha-keyed groups so the same photo set recalls free."""
    sha_by_id = {p["photo_id"]: p["sha256"] for p in subset if p.get("sha256")}
    groups = []
    for pr in proposals:
        shas = sorted({sha_by_id[pid] for pid in pr["photo_ids"] if pid in sha_by_id})
        if not shas:
            continue
        groups.append({"shas": shas, "kind": pr["kind"], "label": pr.get("label"),
                       "confidence": pr.get("confidence"), "meta": pr.get("meta")})
    try:
        cache.remember(key, {"groups": groups}, kind=_CACHE_KIND,
                       model=model, cost_tier="costed")
    except Exception as exc:
        logger.debug("SCB grouping remember failed: %s", exc)


def propose(photos: list[dict], batch: dict, cfg: dict) -> list[dict[str, Any]] | None:
    """Delegate tier-3 grouping to the Brain, recalling a cached result first.

    Returns a (possibly empty) proposal list when the Brain ran or a cached
    result was reused, or ``None`` when the Brain is unreachable / errored so
    :mod:`grouping` can fall back to the standalone OpenRouter transport.
    """
    subset = photos[:vision_common.MAX_PHOTOS]

    # 1) Recall — free. Reuse a prior grouping of this exact photo set.
    cache = _load_vlm_cache() if cfg.get("scb_vlm_cache", True) else None
    key = _content_key(subset) if cache is not None else None
    if cache is not None and key is not None:
        hit = _recall_groups(cache, key, subset)
        if hit is not None:
            return hit

    # 2) Costed dispatch through the Brain's ensemble.
    call = _load_ensemble_call()
    if call is None:
        return None
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
    proposals = vision_common.parse_groups(
        parsed, subset, sent_idx,
        signal="llm_vision", route="scb_dispatch", model=resp.get("model"))

    # 3) Remember — write the fresh result back so this photo set recalls free.
    if cache is not None and key is not None:
        _remember_groups(cache, key, subset, proposals, resp.get("model"))
    return proposals
