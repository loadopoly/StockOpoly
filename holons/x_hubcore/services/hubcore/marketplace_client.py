"""HubCore's client for the GARD marketplace, which owns the Web3 store.

HubCore is the centralization and policy layer: it authenticates the caller,
resolves the organization from the token, and enforces the scope split. It does
not own marketplace state. `gard-marketplace` does -- it is the central location
for Web3 shard and NFT records, and the single writer of both the transactional
store and the Parquet partitions behind it.

Two consequences the code has to honour:

* **One writer.** Embedding the engine inside HubCore *and* running the
  standalone service pointed both at the same SQLite file and the same Parquet
  root. SQLite survives that (measured: two containers, 400 concurrent WAL
  writes across the bind mount, nothing lost). Parquet does not -- it has no
  locking at all, so two appenders to one partition interleave partial files.
* **One front door.** The marketplace is reachable only on the compose bridge.
  Publishing it to the host would let anything on the machine bypass the
  air-lock's authentication and read another organization's hub catalog, which
  is the hole HubCore's `_hub_scope_claims` exists to close.

This client mirrors the engine's method names so the request handlers are
unchanged by where the store lives. The organization is passed as a resolved
header, never re-derived from caller input on the far side.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .errors import HubCoreError, PolicyError

#: Header carrying the organization HubCore already resolved from the bearer
#: token. The marketplace trusts it only alongside the internal secret.
ORG_HEADER = "X-HubCore-Org"

#: Proves the caller is HubCore rather than anything else that reached the
#: bridge. Defence in depth behind the air-lock, not a substitute for it.
AUTH_HEADER = "X-HubCore-Auth"

INTERNAL_SECRET_ENV = "MARKETPLACE_INTERNAL_SECRET"


class MarketplaceUnavailable(HubCoreError):
    """The marketplace service did not answer. Never a mocked result."""

    code = "hubcore.marketplace.unavailable"


class RemoteMarketplace:
    """A duck-typed proxy for :class:`GARDMarketplaceEngine` over HTTP."""

    def __init__(self, base_url: str, secret: Optional[str] = None, timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.secret = secret or os.environ.get(INTERNAL_SECRET_ENV, "")
        self.timeout = float(timeout)

    # -- transport ---------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> Any:
        url = self.base_url + path
        if params:
            pairs = [
                str(k) + "=" + urllib.parse.quote(str(v))
                for k, v in params.items()
                if v is not None
            ]
            if pairs:
                url += "?" + "&".join(pairs)

        headers = {"Accept": "application/json", "User-Agent": "HubCore/10.1 marketplace-client"}
        if org_id:
            headers[ORG_HEADER] = org_id
        if self.secret:
            headers[AUTH_HEADER] = self.secret
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(body).get("error") or body
            except json.JSONDecodeError:
                detail = body
            if exc.code in (401, 403):
                raise PolicyError("marketplace refused the request: " + str(detail)[:200])
            raise HubCoreError(
                "marketplace returned HTTP " + str(exc.code) + ": " + str(detail)[:200]
            )
        except Exception as exc:
            raise MarketplaceUnavailable(
                "the marketplace service at " + self.base_url + " did not answer ("
                + type(exc).__name__ + "). No cached or partial catalog is "
                "substituted: an invented listing is indistinguishable from a real one"
            ) from exc

    # -- reads -------------------------------------------------------------

    def get_catalog(
        self,
        scope: str = "shared",
        axis: Optional[str] = None,
        org_id: Optional[str] = None,
        domain: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[Any]:
        body = self._call(
            "GET",
            "/api/v1/marketplace/catalog",
            params={
                "scope": scope, "axis": axis, "org_id": org_id,
                "domain": domain, "category": category, "limit": limit,
            },
            org_id=org_id,
        )
        return [_AssetView(a) for a in (body or {}).get("assets", [])]

    def get_hub_stats(self, org_id: Optional[str] = None) -> Dict[str, Any]:
        return self._call(
            "GET", "/api/v1/marketplace/hub/stats",
            params={"org_id": org_id}, org_id=org_id,
        )

    def get_stats(self) -> Dict[str, Any]:
        return self._call("GET", "/api/v1/marketplace/stats")

    def get_recent_transactions(self, limit: int = 50) -> List[Dict[str, Any]]:
        body = self._call("GET", "/api/v1/marketplace/royalties", params={"limit": limit})
        return (body or {}).get("transactions", body if isinstance(body, list) else [])

    # -- writes ------------------------------------------------------------

    def tokenize_asset(self, **kwargs: Any) -> Any:
        body = self._call("POST", "/api/v1/marketplace/tokenize", payload=kwargs)
        return _AssetView((body or {}).get("asset", body))

    def tokenize_supply_chain_asset(self, **kwargs: Any) -> Any:
        body = self._call(
            "POST", "/api/v1/marketplace/hub/tokenize",
            payload=kwargs, org_id=kwargs.get("org_id"),
        )
        return _AssetView((body or {}).get("asset", body))

    def execute_hub_internal_transfer(self, **kwargs: Any) -> Dict[str, Any]:
        return self._call(
            "POST", "/api/v1/marketplace/hub/transfer",
            payload=kwargs, org_id=kwargs.get("org_id"),
        )

    def sync_hub_floor_manifest(self, **kwargs: Any) -> Dict[str, Any]:
        return self._call(
            "POST", "/api/v1/marketplace/hub/sync",
            payload=kwargs, org_id=kwargs.get("org_id"),
        )

    def execute_trade(self, **kwargs: Any) -> Dict[str, Any]:
        return self._call("POST", "/api/v1/marketplace/trade", payload=kwargs)

    def record_on_chain_mint(self, **kwargs: Any) -> Dict[str, Any]:
        return self._call("POST", "/api/v1/marketplace/web3/mint", payload=kwargs)

    def create_gard_envelope(self, **kwargs: Any) -> Dict[str, Any]:
        return self._call("POST", "/api/v1/marketplace/gard/shard", payload=kwargs)

    def verify_and_reassemble_envelope(self, **kwargs: Any) -> Dict[str, Any]:
        return self._call("POST", "/api/v1/marketplace/gard/verify", payload=kwargs)

    # -- health ------------------------------------------------------------

    def ping(self) -> Dict[str, Any]:
        return self._call("GET", "/health")


class _AssetView(dict):
    """A remote asset that still answers ``.to_dict()`` and attribute access.

    The request handlers were written against the in-process engine's dataclass.
    Keeping that shape means moving the store did not require rewriting them,
    and a handler cannot accidentally start depending on locality.
    """

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
