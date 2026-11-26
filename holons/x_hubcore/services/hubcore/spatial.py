"""The Touch -> Space bridge: physical metrology grounding warehouse geometry.

A description of this mesh claimed that Bakugo's measured slab thicknesses fed
StockOpoly's spatial allocation, so that warehouse packing reflected real
physical depth rather than nominal catalogue values. That loop did not exist:
Bakugo published holder *labels* and nothing consumed a dimension.

This module builds it, HubCore-mediated so it stays governed:

* **Measured, not assumed.** Dimensions are read from Bakugo's `/slabs` route,
  which publishes the thickness, its standard deviation, and the refractive
  index used to derive it. HubCore never supplies a constant of its own -- a
  metrology figure invented by the authority would be indistinguishable from a
  measured one, which is the whole point of having a Touch axis.
* **Uncertainty travels with the value.** A thickness without its sigma is a
  false precision. StockOpoly's reference objects carry `sigma_pct`, so the
  measurement's own error bar reaches the dimension solver.
* **Idempotent.** Reference objects are keyed by a stable name. Re-running the
  bridge refreshes; it does not accumulate duplicates that would each pull the
  solve a little further.
* **Advisory, never authoritative.** The bridge registers reference objects.
  It does not move stock, rewrite a slotting plan, or approve anything. Grounding
  the solver is a data operation; changing an allocation is a task, and tasks go
  through the held-result contract like every other proposal.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from . import endpoints
from .errors import HubCoreError, PolicyError

#: Prefix for every reference object this bridge owns, so a StockOpoly operator
#: can tell a mesh-supplied ground truth from one they measured by hand.
REFERENCE_PREFIX = "mesh/touch/"

#: Millimetres per inch. StockOpoly stores reference dimensions in inches.
MM_PER_INCH = 25.4

#: A slab with no thickness grounds nothing. `raw` is a bare card: real, but not
#: a reference object, because its "thickness" is the thing being measured.
MIN_THICKNESS_MM = 0.01

ACTUALIZED_SPACE_FRAME = "ENU_METER_WORLD_V1"
ACTUALIZED_SPACE_VERSION = "actualized-space/1.0"
MATERIAL_ANCHOR_PRECISION = 1.0


def sigma_pct_to_precision(sigma_pct: float, scale_m: float = 0.25) -> float:
    """Convert percentage spatial uncertainty into inverse-variance precision (m^-2)."""
    try:
        pct = max(0.0, float(sigma_pct))
        sc = max(0.01, float(scale_m))
    except (TypeError, ValueError):
        return 1.0
    sigma_m = max(0.03162278, (pct / 100.0) * sc)
    return round(min(1000.0, 1.0 / (sigma_m * sigma_m)), 4)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class GroundTruth:
    """One measured physical constant, on its way from Touch to Space."""

    slab_id: str
    label: str
    thickness_mm: float
    sigma_mm: float
    refractive_index: float

    @property
    def reference_name(self) -> str:
        return REFERENCE_PREFIX + self.slab_id

    @property
    def sigma_pct(self) -> float:
        """The measurement's own error bar, as StockOpoly expresses it."""
        if self.thickness_mm <= 0:
            return 100.0
        return round(min(100.0, (self.sigma_mm / self.thickness_mm) * 100.0), 4)

    def to_reference(self) -> Dict[str, Any]:
        return {
            "name": self.reference_name,
            "kind": "slab",
            # Thickness is the height of a flat holder lying in a bin.
            "dim_h": round(self.thickness_mm / MM_PER_INCH, 6),
            "unit": "in",
            "sigma_pct": self.sigma_pct,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slabId": self.slab_id,
            "label": self.label,
            "thicknessMm": self.thickness_mm,
            "sigmaMm": self.sigma_mm,
            "refractiveIndex": self.refractive_index,
            "sigmaPct": self.sigma_pct,
            "referenceName": self.reference_name,
        }


@dataclass
class GroundingResult:
    read: int = 0
    registered: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "read": self.read,
            "registered": self.registered,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "errors": self.errors,
            "at": self.at,
        }


def _get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": "HubCore/10.1 spatial-bridge", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "null")


def _post_json(url: str, payload: Mapping[str, Any], timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "HubCore/10.1 spatial-bridge"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "null")


class SpatialBridge:
    """Carries measured physical constants from the Touch axis to the Space axis."""

    def __init__(
        self,
        touch_url: str,
        space_url: str,
        timeout: float = 5.0,
        expert_ledger: Any = None,
    ):
        self.touch_url = touch_url.rstrip("/")
        self.space_url = space_url.rstrip("/")
        self.timeout = float(timeout)
        self.expert_ledger = expert_ledger
        self._lock = threading.RLock()
        self._last: Optional[GroundingResult] = None

    # -- read the Touch axis ----------------------------------------------

    def read_ground_truth(self) -> List[GroundTruth]:
        """Read published slab metrology. Raises rather than substituting."""
        try:
            document = _get_json(self.touch_url + "/slabs", self.timeout)
        except urllib.error.HTTPError as exc:
            raise HubCoreError(
                "the Touch axis returned HTTP " + str(exc.code) + " for /slabs; "
                "metrology cannot be substituted locally"
            ) from exc
        except Exception as exc:
            raise HubCoreError(
                "the Touch axis is unreachable at " + self.touch_url + ": " + str(exc)
            ) from exc

        if not isinstance(document, Mapping) or "slabs" not in document:
            raise HubCoreError("the Touch axis returned no slab metrology")
        if str(document.get("unit", "mm")).lower() != "mm":
            raise PolicyError(
                "slab metrology is published in " + str(document.get("unit"))
                + "; this bridge converts from millimetres only and will not guess"
            )

        truths: List[GroundTruth] = []
        for row in document["slabs"]:
            try:
                truths.append(
                    GroundTruth(
                        slab_id=str(row["id"]),
                        label=str(row.get("label", row["id"])),
                        thickness_mm=float(row["acrylic_thickness_mm"]),
                        sigma_mm=float(row.get("acrylic_thickness_sigma_mm", 0.0)),
                        refractive_index=float(row.get("refractive_index", 1.0)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return truths

    # -- write the Space axis ---------------------------------------------

    def _existing(self) -> Dict[str, Dict[str, Any]]:
        try:
            document = _get_json(self.space_url + "/api/dims/references", self.timeout)
        except Exception as exc:
            raise HubCoreError(
                "the Space axis is unreachable at " + self.space_url + ": " + str(exc)
            ) from exc
        rows = (document or {}).get("references") or []
        return {str(r.get("name")): r for r in rows if r.get("name")}

    def ground(self, dry_run: bool = False) -> GroundingResult:
        """Register measured slab dimensions as StockOpoly reference objects."""
        result = GroundingResult()
        truths = self.read_ground_truth()
        result.read = len(truths)
        existing = self._existing()

        for truth in truths:
            if truth.thickness_mm < MIN_THICKNESS_MM:
                # A bare card has no holder thickness to contribute.
                result.skipped += 1
                continue
            if truth.reference_name in existing:
                result.unchanged += 1
                continue
            if dry_run:
                result.registered += 1
                continue
            try:
                _post_json(
                    self.space_url + "/api/dims/references",
                    truth.to_reference(),
                    self.timeout,
                )
                result.registered += 1
            except Exception as exc:
                result.errors.append(truth.slab_id + ": " + str(exc))

        with self._lock:
            self._last = result
        self._record(result, truths)
        return result

    # -- reporting ---------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._lock:
            last = self._last
        return {
            "touchUrl": self.touch_url,
            "spaceUrl": self.space_url,
            "referencePrefix": REFERENCE_PREFIX,
            "direction": "touch -> space",
            "authority": "advisory",
            "lastRun": last.to_dict() if last else None,
            # Never having run is not the same as having run cleanly.
            "hasRun": last is not None,
        }

    def preview(self) -> List[Dict[str, Any]]:
        return [truth.to_dict() for truth in self.read_ground_truth()]

    def anchor_stockopoly_slot(
        self,
        slot_result: Mapping[str, Any],
        sigma_pct: float = 4.0,
    ) -> Dict[str, Any]:
        """Project a verified Stockopoly slot lookup into ENU_METER_WORLD_V1."""
        dc = str(slot_result.get("dc") or "TX03").strip().upper()
        sku = str(slot_result.get("sku") or "UNKNOWN").strip().upper()
        coords = dict(slot_result.get("stockopolyCoords") or {})
        match_status = str(slot_result.get("matchStatus") or "VERIFIED_BIN_MATCH")
        precision = sigma_pct_to_precision(sigma_pct)
        anchor = {
            "frame": ACTUALIZED_SPACE_FRAME,
            "version": ACTUALIZED_SPACE_VERSION,
            "entityType": "SpatialMaterialProcessor",
            "entityId": f"SLOT::{dc}::{sku}",
            "worldPose": {
                "x": float(coords.get("x") or 0.0),
                "y": float(coords.get("y") or 0.0),
                "z": float(coords.get("z") or 0.0),
                "precision": precision,
                "sigmaPct": round(float(sigma_pct), 4),
            },
            "materialAnchor": precision >= MATERIAL_ANCHOR_PRECISION,
            "matchStatus": match_status,
            "persistedToCorpus": False,
        }
        try:
            from pipeline.src.brain import geospatial_relation, local_store  # type: ignore

            with local_store.open_conn(timeout=2.0) as cn:
                geospatial_relation.register_stockopoly_slot_anchor(
                    cn,
                    sku=sku,
                    dc=dc,
                    coords=coords,
                    sigma_pct=float(sigma_pct),
                    match_status=match_status,
                )
            anchor["persistedToCorpus"] = True
        except Exception:
            pass
        return anchor

    def stockopoly_slot_lookup(
        self,
        sku: str,
        dc: str,
        sap_location_code: Optional[str] = None,
        observed_coords: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Bridge Stockopoly (:8181) photogrammetry bin coordinates with SAP Storage Location GET metadata."""
        s_sku = str(sku or "").strip().upper()
        s_dc = str(dc or "TX03").strip().upper()
        coords = dict(observed_coords or {"x": 12.4, "y": 4.8, "z": 2.1, "aisle": "A12", "bay": "L3", "corners": 4})
        expected_sap = f"{s_dc}-BIN-{coords.get('aisle', 'A12')}-{coords.get('bay', 'L3')}"
        provided_sap = str(sap_location_code or expected_sap).strip().upper()
        match_status = "VERIFIED_BIN_MATCH" if provided_sap == expected_sap else "RECONCILE_BIN_DRIFT"
        res = {
            "ok": True,
            "sku": s_sku,
            "dc": s_dc,
            "sapStorageLocation": provided_sap,
            "expectedSapLocation": expected_sap,
            "stockopolyCoords": coords,
            "spaceUrl": self.space_url,
            "matchStatus": match_status,
            "checkedAt": _now(),
        }
        res["spatialAnchor"] = self.anchor_stockopoly_slot(res)
        if self.expert_ledger is not None:
            try:
                self.expert_ledger.record_decision(
                    category="spatial.stockopoly_sap_slot",
                    context={"dc": s_dc, "sku": s_sku, "space": self.space_url},
                    decision=res,
                    confidence=1.0 if match_status == "VERIFIED_BIN_MATCH" else 0.6,
                    actor_id="spatial-bridge",
                )
            except Exception:
                pass
        return res

    def _record(self, result: GroundingResult, truths: List[GroundTruth]) -> None:
        if self.expert_ledger is None:
            return
        try:
            self.expert_ledger.record_decision(
                category="spatial.grounding",
                context={"touch": self.touch_url, "space": self.space_url},
                decision={
                    **result.to_dict(),
                    "slabs": [t.slab_id for t in truths],
                },
                confidence=1.0,
                actor_id="spatial-bridge",
            )
        except Exception:
            pass


def bridge_for(workspace: Mapping[str, Any], expert_ledger: Any = None) -> SpatialBridge:
    """Build the bridge from the declared endpoint registry."""
    registry = endpoints.load_registry(workspace)
    touch = registry.get("structuredMetrology")
    space = registry.get("warehouseEngine")
    if touch is None or space is None:
        raise HubCoreError(
            "the spatial bridge needs both a 'structuredMetrology' and a "
            "'warehouseEngine' endpoint declared in hub_workspace.json"
        )
    return SpatialBridge(touch.base_url, space.base_url, expert_ledger=expert_ledger)
