"""Hub Link & State Parser — connects StockOpoly directly to HubCore.

Parses HubCore's:
1. Spatial Bridge Ground Truths:
   - Reads /api/v1/spatial/ground-truth (Bakugo-derived acrylic slab metrology)
   - Synchronizes slabs into StockOpoly reference_objects with measurement sigmas.
2. Cognitive Axes Registry:
   - Reads /api/v1/axes to verify touch, vision, space, and perception health.
3. Bidirectional Grounding:
   - Invokes /api/v1/spatial/ground when triggered from StockOpoly.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import dims
from .store import open_conn

logger = logging.getLogger(__name__)

DEFAULT_HUB_URL = os.environ.get("HUBCORE_URL") or "http://loadopoly-hubcore:8000"
FALLBACK_HUB_URLS = [
    "http://127.0.0.1:8000",
    "http://host.docker.internal:8000",
]

import time

_TIMEOUT = 4.0
MM_PER_INCH = 25.4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_get_json(url: str, timeout: float = _TIMEOUT, token: Optional[str] = None) -> tuple[bool, Any]:
    headers = {"Accept": "application/json", "User-Agent": "StockOpoly/hub-link"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", "ignore")
            return True, json.loads(raw)
    except Exception as exc:
        return False, str(exc)


def _http_post_json(url: str, payload: dict[str, Any], timeout: float = _TIMEOUT, token: Optional[str] = None) -> tuple[bool, Any]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "StockOpoly/hub-link"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", "ignore")
            try:
                return True, json.loads(raw)
            except Exception:
                return True, raw
    except Exception as exc:
        return False, str(exc)


class HubConnectionManager:
    """Manages the connection from StockOpoly to HubCore."""

    def __init__(self, base_url: str = DEFAULT_HUB_URL):
        self.base_url = base_url.rstrip("/")
        self.fallbacks = [u.rstrip("/") for u in FALLBACK_HUB_URLS]
        self._last_sync_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._synced_slabs: int = 0
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def resolve_hub_url(self) -> Optional[str]:
        """Find the reachable HubCore host."""
        candidates = [self.base_url] + self.fallbacks
        for cand in candidates:
            ok, res = _http_get_json(f"{cand}/health", timeout=1.5)
            if ok:
                return cand
        return None

    def get_token(self, hub_url: str) -> Optional[str]:
        """Mint or reuse a scoped HubCore session token."""
        now = time.time()
        if self._token and now < self._token_expires_at - 30:
            return self._token
        payload = {
            "orgId": "org-blackbulls-hideout-01",
            "siteId": "site-default",
            "deviceId": "dev-default",
            "repoId": "default",
            "userId": "stockopoly",
            "role": "Requester",
        }
        ok, res = _http_post_json(f"{hub_url}/api/v1/session", payload, timeout=_TIMEOUT)
        if ok and isinstance(res, dict) and "token" in res:
            self._token = res["token"]
            self._token_expires_at = float(res.get("expiresAt") or (now + 900))
            return self._token
        return None

    def parse_hub_spatial_ground_truth(self) -> dict[str, Any]:
        """Fetch ground-truth slabs from HubCore and parse into reference_objects."""
        hub = self.resolve_hub_url()
        if not hub:
            self._last_error = "HubCore unreachable"
            return {"ok": False, "error": self._last_error}

        token = self.get_token(hub)
        url = f"{hub}/api/v1/spatial/ground-truth"
        ok, data = _http_get_json(url, timeout=_TIMEOUT, token=token)
        if not ok or not isinstance(data, dict):
            self._last_error = f"Failed to fetch {url}: {data}"
            return {"ok": False, "error": self._last_error}

        slabs = data.get("groundTruth", [])
        if not isinstance(slabs, list):
            slabs = []

        # Read existing references to avoid duplicates
        existing = {r["name"]: r for r in dims.list_reference_objects()}
        added = 0
        updated = 0

        cn = open_conn()
        try:
            for s in slabs:
                name = s.get("referenceName") or f"mesh/touch/{s.get('slabId')}"
                thickness_mm = float(s.get("thicknessMm", 0.0))
                sigma_pct = float(s.get("sigmaPct", 1.0))
                label = s.get("label", "")
                refractive_index = s.get("refractiveIndex", 1.0)

                dim_h = round(thickness_mm / MM_PER_INCH, 6)
                notes = f"HubCore slab {s.get('slabId')} ({label}), n={refractive_index}"

                if name in existing:
                    # Update dimensions if needed
                    cn.execute(
                        "UPDATE reference_objects SET dim_h=?, sigma_pct=?, notes=? WHERE name=?",
                        (dim_h, sigma_pct, notes, name),
                    )
                    updated += 1
                else:
                    cn.execute(
                        "INSERT INTO reference_objects(name, kind, dim_l, dim_w, dim_h, unit, sigma_pct, notes) "
                        "VALUES (?, 'slab', NULL, NULL, ?, 'in', ?, ?)",
                        (name, dim_h, sigma_pct, notes),
                    )
                    added += 1
            cn.commit()
        finally:
            cn.close()

        self._last_sync_at = _now()
        self._synced_slabs = len(slabs)
        self._last_error = None

        return {
            "ok": True,
            "hub_url": hub,
            "slabs_read": len(slabs),
            "references_added": added,
            "references_updated": updated,
            "synced_at": self._last_sync_at,
        }

    def trigger_hub_grounding(self) -> dict[str, Any]:
        """Trigger HubCore's /api/v1/spatial/ground and then parse ground truth."""
        hub = self.resolve_hub_url()
        if not hub:
            return {"ok": False, "error": "HubCore unreachable"}

        token = self.get_token(hub)
        ok, ground_res = _http_post_json(f"{hub}/api/v1/spatial/ground", {"dryRun": False}, token=token)
        sync_res = self.parse_hub_spatial_ground_truth()

        return {
            "ok": ok and sync_res.get("ok", False),
            "hub_ground_response": ground_res,
            "stockopoly_sync": sync_res,
        }

    def status(self) -> dict[str, Any]:
        hub = self.resolve_hub_url()
        axes_info = None
        if hub:
            token = self.get_token(hub)
            ok, a_res = _http_get_json(f"{hub}/api/v1/axes", timeout=2.0, token=token)
            if ok and isinstance(a_res, dict):
                axes_info = a_res.get("axes")

        return {
            "configured_hub_url": self.base_url,
            "resolved_hub_url": hub,
            "connected": hub is not None,
            "last_sync_at": self._last_sync_at,
            "synced_slabs_count": self._synced_slabs,
            "last_error": self._last_error,
            "hub_axes": axes_info,
        }


# Global default instance
_DEFAULT_HUB_MANAGER: Optional[HubConnectionManager] = None


def get_hub_manager() -> HubConnectionManager:
    global _DEFAULT_HUB_MANAGER
    if _DEFAULT_HUB_MANAGER is None:
        _DEFAULT_HUB_MANAGER = HubConnectionManager()
    return _DEFAULT_HUB_MANAGER
