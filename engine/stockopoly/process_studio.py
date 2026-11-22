"""process_studio — StockOpoly's v-ADMIN client for QUIPU's x_ProcessStudio holon.

StockOpoly's VLM/LLM tier (the grouping cascade, tier 3) can *want* real-world
processes run — a replenishment flow, a slotting task, a message — which live
behind QUIPU's x_ProcessStudio holon as Activepieces MCP tools. This module
STAGES ``ActionProposal``s on ``POST /process_studio/propose``; staging is the
whole of its authority (v-ADMIN). Authorisation (the operator's grant_ref,
V10-SEC-010) and realisation (r-ADMIN → Activepieces ``tools/call``) happen on
QUIPU, never here. See QUIPU ``docs/X_PROCESS_STUDIO.md``.

Wire shape (schema ``process_studio.propose/1``)::

    {"source": "stockopoly", "tool": "...", "arguments": {...},
     "reason": "...", "meta": {"schema": ..., "vlm": {...}}}

Writes are signed with QUIPU's edge canonical form (HMAC-SHA256 over
``METHOD\\nPATH\\nTS\\nsha256(body)\\nIDEM``; headers ``X-Quipu-Source`` /
``-Timestamp`` / ``-Signature`` / ``-Idempotency``) when
``QUIPU_EDGE_KEY_STOCKOPOLY`` or ``QUIPU_EDGE_KEY`` is set, so they survive
``QUIPU_EDGE_AUTH=enforce``; unsigned they ride the edge's ``record`` mode.
The idempotency key is derived from the body, so a spool replay dedupes on
QUIPU instead of staging twice.

Dispatch rides the existing ``dims.quipu_safety_net`` (circuit breaker +
spool + flush): an unreachable QUIPU never blocks the engine, and nothing
wanted is lost. Stdlib only; everything degrades to a clean no-op.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from .dims import quipu_safety_net

logger = logging.getLogger(__name__)

SOURCE = "stockopoly"
PROPOSE_PATH = "/process_studio/propose"
SCHEMA = "process_studio.propose/1"
DEFAULT_REGROUP_TOOL = os.environ.get("STOCKOPOLY_STUDIO_TOOL", "stockopoly_regroup")

_TIMEOUT_S = 8.0

H_SOURCE, H_TS, H_SIG, H_IDEM = ("X-Quipu-Source", "X-Quipu-Timestamp",
                                 "X-Quipu-Signature", "X-Quipu-Idempotency")


def quipu_url() -> str:
    return (os.environ.get("QUIPU_URL") or "http://quipu:7100").rstrip("/")


def _edge_key() -> Optional[str]:
    """The source's edge key (hex), per-source name first — or None (unsigned)."""
    raw = (os.environ.get("QUIPU_EDGE_KEY_STOCKOPOLY")
           or os.environ.get("QUIPU_EDGE_KEY") or "").strip()
    if not raw:
        return None
    try:
        bytes.fromhex(raw)
    except ValueError:
        logger.debug("process_studio: edge key is not hex; sending unsigned")
        return None
    return raw


def idempotency_key(path: str, body: bytes) -> str:
    """Byte-compatible with QUIPU edge_client.idempotency_key(source, path, body)."""
    return hashlib.sha256(f"{SOURCE}\n{path}\n".encode() + body).hexdigest()[:40]


def sign_headers(key_hex: str, method: str, path: str, body: bytes,
                 idem: str | None = None, ts: float | None = None) -> dict[str, str]:
    """QUIPU's edge canonical form, vendored byte-for-byte (edge_client.sign_headers)."""
    t = str(int(time.time() if ts is None else ts))
    msg = f"{method.upper()}\n{path}\n{t}\n{hashlib.sha256(body).hexdigest()}"
    if idem:
        msg += f"\n{idem}"
    mac = hmac.new(bytes.fromhex(key_hex), msg.encode(), hashlib.sha256).hexdigest()
    out = {H_SOURCE: SOURCE, H_TS: t, H_SIG: mac}
    if idem:
        out[H_IDEM] = idem
    return out


def _dispatch(url: str, payload: Mapping[str, Any], timeout: float = _TIMEOUT_S) -> tuple[bool, Any]:
    """Signing dispatcher in the QuipuSafetyNet shape: (ok, response-or-error)."""
    body = json.dumps(payload, default=str).encode("utf-8")
    path = urlparse(url).path or PROPOSE_PATH          # the signed canonical path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    idem = idempotency_key(path, body)
    key = _edge_key()
    if key:
        headers.update(sign_headers(key, "POST", path, body, idem=idem))
    else:
        headers[H_SOURCE] = SOURCE
        headers[H_IDEM] = idem
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", "ignore")
            try:
                return True, json.loads(raw)
            except ValueError:
                return True, raw
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        return False, f"HTTP {exc.code}: {detail or getattr(exc, 'reason', '')}"
    except Exception as exc:
        return False, str(exc)


def propose_action(tool: str, arguments: Mapping[str, Any] | None = None, *,
                   reason: str = "", vlm: Mapping[str, Any] | None = None,
                   meta: Mapping[str, Any] | None = None,
                   url: str | None = None, net=None) -> dict[str, Any]:
    """Stage one ActionProposal on QUIPU (held for the operator; never executed
    here). Returns the safety-net verdict: ``staged`` True when QUIPU answered,
    ``spooled`` True when the net kept it for a later flush."""
    name = (tool or "").strip()
    if not name:
        return {"ok": False, "staged": False, "error": "tool required"}
    m: dict[str, Any] = {"schema": SCHEMA}
    if vlm:
        m["vlm"] = dict(vlm)
    if meta:
        m.update(dict(meta))
    payload = {"source": SOURCE, "tool": name, "arguments": dict(arguments or {}),
               "reason": str(reason or ""), "meta": m}
    target = (url.rstrip("/") if url else quipu_url()) + PROPOSE_PATH
    sn = net if net is not None else quipu_safety_net.get_safety_net()
    out = sn.execute(target, payload, _dispatch)
    resp = out.get("response") if isinstance(out, dict) else None
    staged = bool(isinstance(resp, dict) and resp.get("ok"))
    verdict = {"ok": bool(out.get("ok", False)), "staged": staged,
               "spooled": bool(out.get("spooled")), "tool": name, **out}
    if staged and isinstance(resp.get("proposal"), dict):
        verdict["proposal_id"] = resp["proposal"].get("proposal_id")
    return verdict


def propose_regroup(batch_id: str, proposals: list[Mapping[str, Any]], *,
                    tool: str = DEFAULT_REGROUP_TOOL, net=None) -> dict[str, Any]:
    """Shape a tier-3 grouping outcome into a staged studio proposal: the VLM
    evidence rides in ``meta.vlm`` so the operator authorises with it in view."""
    kinds: dict[str, int] = {}
    conf = 0.0
    for p in proposals or []:
        kinds[str(p.get("kind") or "?")] = kinds.get(str(p.get("kind") or "?"), 0) + 1
        try:
            conf = max(conf, float(p.get("confidence") or 0.0))
        except (TypeError, ValueError):
            pass
    vlm = {"batch_id": batch_id, "proposals": len(proposals or []),
           "kinds": kinds, "confidence_max": round(conf, 3)}
    return propose_action(tool, {"batch_id": batch_id},
                          reason=f"tier-3 vision proposed {len(proposals or [])} "
                                 f"group(s) for batch {batch_id[:8]}",
                          vlm=vlm, net=net)


def flush(net=None, max_batch: int = 50) -> dict[str, Any]:
    """Drain anything the safety net spooled while QUIPU was unreachable."""
    sn = net if net is not None else quipu_safety_net.get_safety_net()
    return sn.flush(_dispatch, max_batch=max_batch)


def status() -> dict[str, Any]:
    return {"url": quipu_url() + PROPOSE_PATH, "signed": _edge_key() is not None,
            "safety_net": quipu_safety_net.get_safety_net().status()}
