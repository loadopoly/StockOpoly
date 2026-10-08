"""AGV (Automated Guided Vehicle) Fleet & Material Transport Engine.

Manages autonomous mobile robots executing material movements in the warehouse:
1. 3D Waypoint Path Planning:
   - Uses solved warehouse geometry (aisle_x, bay_w, level_h).
   - Generates collision-free waypoints: bin -> aisle front -> cross aisle -> destination cell.
2. Mission Dispatch & Lifecycle:
   - Integrates with SCB's ``body_directives`` table for physical execution orders.
   - Updates warehouse inventory upon arrival (debits origin bin, credits destination WIP).
3. Symbiosis Integration:
   - Responds to Svarog Forge & ACRE manufacturing material requests.
"""
from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import events, locations as loc_mod, scb_link, settings
from .store import open_conn

logger = logging.getLogger(__name__)

_DEFAULT_SPEED_IPS = 48.0   # 48 inches/second ≈ 4 ft/s ≈ 2.7 mph
_PICK_TIME_S = 30.0        # Time to lift/secure pallet
_DROP_TIME_S = 25.0        # Time to place and verify payload


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Waypoint:
    x: float
    y: float
    z: float
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {"x": round(self.x, 2), "y": round(self.y, 2), "z": round(self.z, 2), "label": self.label}


@dataclass
class AGVMission:
    mission_id: str
    demand_id: str
    part_number: str
    qty: float
    from_location: str
    to_location: str
    waypoints: list[Waypoint] = field(default_factory=list)
    travel_distance_in: float = 0.0
    est_duration_s: float = 0.0
    status: str = "pending"   # pending | dispatched | in_transit | completed | failed
    dispatched_at: Optional[str] = None
    completed_at: Optional[str] = None
    agv_id: str = "AGV-01"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["waypoints"] = [w.to_dict() if isinstance(w, Waypoint) else w for w in self.waypoints]
        return d


class AGVFleetManager:
    """Coordinates AGV material missions between warehouse racks and Hub manufacturing."""

    def __init__(self):
        self._missions: dict[str, AGVMission] = {}

    def get_location_coords(self, code: str) -> Optional[Tuple[float, float, float]]:
        """Look up 3D coordinates for a location code."""
        code_u = code.strip().upper()
        cn = open_conn()
        try:
            row = cn.execute(
                "SELECT x, y, z FROM locations WHERE location_code=?", (code_u,)
            ).fetchone()
            if row and row["x"] is not None:
                return float(row["x"]), float(row["y"]), float(row["z"] or 0.0)
            if code_u.startswith("HUB") or code_u.startswith("DOCK"):
                # Virtual staging coordinates near origin
                return 0.0, 0.0, 0.0
            return None
        finally:
            cn.close()

    def plan_path(self, from_code: str, to_code: str) -> Tuple[list[Waypoint], float]:
        """Compute 3D waypoints and total distance for an AGV path."""
        c_src = self.get_location_coords(from_code) or (0.0, 100.0, 0.0)
        c_dst = self.get_location_coords(to_code) or (0.0, 0.0, 0.0)

        waypoints: list[Waypoint] = []
        # 1. Source bin
        waypoints.append(Waypoint(c_src[0], c_src[1], c_src[2], f"Pick at {from_code}"))

        # 2. Lower to ground level for transport
        if c_src[2] > 0.0:
            waypoints.append(Waypoint(c_src[0], c_src[1], 0.0, "Lower mast to floor"))

        # 3. Travel to aisle front (y=0)
        waypoints.append(Waypoint(c_src[0], 0.0, 0.0, "Aisle exit to cross-way"))

        # 4. Cross-aisle traverse to destination x
        if abs(c_src[0] - c_dst[0]) > 1.0:
            waypoints.append(Waypoint(c_dst[0], 0.0, 0.0, f"Cross-way to {to_code} corridor"))

        # 5. Travel along destination aisle to target y
        if abs(c_dst[1]) > 1.0:
            waypoints.append(Waypoint(c_dst[0], c_dst[1], 0.0, "Destination approach"))

        # 6. Lift to target z
        waypoints.append(Waypoint(c_dst[0], c_dst[1], c_dst[2], f"Deposit at {to_code}"))

        # Compute total travel distance
        dist = 0.0
        for i in range(len(waypoints) - 1):
            w1, w2 = waypoints[i], waypoints[i + 1]
            seg = math.sqrt((w2.x - w1.x)**2 + (w2.y - w1.y)**2 + (w2.z - w1.z)**2)
            dist += seg

        return waypoints, round(dist, 2)

    def dispatch_material_mission(
        self,
        part_number: str,
        qty: float,
        from_location: str,
        to_location: str = "HUB-ASSEMBLY",
        demand_id: Optional[str] = None,
        agv_id: str = "AGV-01",
    ) -> AGVMission:
        """Create and dispatch an AGV transport mission."""
        mid = f"mission-agv-{uuid.uuid4().hex[:8]}"
        did = demand_id or f"demand-{uuid.uuid4().hex[:6]}"

        waypoints, dist = self.plan_path(from_location, to_location)
        est_time = round(_PICK_TIME_S + _DROP_TIME_S + (dist / _DEFAULT_SPEED_IPS), 1)

        mission = AGVMission(
            mission_id=mid,
            demand_id=did,
            part_number=part_number,
            qty=qty,
            from_location=from_location,
            to_location=to_location,
            waypoints=waypoints,
            travel_distance_in=dist,
            est_duration_s=est_time,
            status="dispatched",
            dispatched_at=_now(),
            agv_id=agv_id,
        )

        self._missions[mid] = mission

        # Emit actionable directive to SCB body_directives
        scb_link.emit_body_directive(
            title=f"AGV Transport {agv_id}: Deliver {qty}x {part_number} to {to_location}",
            why_it_matters=f"Supplies Svarog ACRE manufacturing build demand ({did}) from bin {from_location}.",
            do_this=f"AGV navigates {len(waypoints)} waypoints ({dist:.0f} in, ~{est_time:.0f}s).",
            owner_role="AGV",
            priority=0.90,
            severity="act",
            fingerprint=f"agv:mission:{mid}",
            evidence={"mission_id": mid, "part_number": part_number, "qty": qty, "distance_in": dist},
        )

        events.record(
            "agv_mission_dispatched",
            mission.to_dict(),
            title=f"AGV Mission {mid} dispatched for {part_number}",
            signal=0.80,
        )

        return mission

    def complete_mission(self, mission_id: str) -> dict[str, Any]:
        """Mark AGV mission completed, transfer physical inventory, and report."""
        if mission_id not in self._missions:
            raise KeyError(f"Unknown mission {mission_id}")

        mission = self._missions[mission_id]
        mission.status = "completed"
        mission.completed_at = _now()

        # Update physical inventory: deduct from origin, add to destination
        cn = open_conn()
        try:
            # Check onhand at from_location
            row = cn.execute(
                "SELECT qty_oh FROM inventory WHERE part_number=? AND location_code=?",
                (mission.part_number, mission.from_location),
            ).fetchone()

            if row:
                current_qty = float(row["qty_oh"])
                rem = max(0.0, current_qty - mission.qty)
                cn.execute(
                    "UPDATE inventory SET qty_oh=? WHERE part_number=? AND location_code=?",
                    (rem, mission.part_number, mission.from_location),
                )
            else:
                current_qty = 0.0

            # Upsert into destination location
            dest_row = cn.execute(
                "SELECT qty_oh FROM inventory WHERE part_number=? AND location_code=?",
                (mission.part_number, mission.to_location),
            ).fetchone()

            if dest_row:
                new_dest_qty = float(dest_row["qty_oh"]) + mission.qty
                cn.execute(
                    "UPDATE inventory SET qty_oh=? WHERE part_number=? AND location_code=?",
                    (new_dest_qty, mission.part_number, mission.to_location),
                )
            else:
                cn.execute(
                    "INSERT INTO inventory(part_number, location_code, qty_oh, as_of) VALUES (?, ?, ?, ?)",
                    (mission.part_number, mission.to_location, mission.qty, _now()),
                )
            cn.commit()
        finally:
            cn.close()

        # Log completion to SCB
        scb_link.log_learning(
            "agv_delivery_completed",
            f"AGV {mission.agv_id} delivered {mission.qty}x {mission.part_number} to {mission.to_location}",
            mission.to_dict(),
            signal=0.85,
        )

        return {"ok": True, "mission": mission.to_dict()}

    def list_missions(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self._missions.values()]

    def status(self) -> dict[str, Any]:
        return {
            "missions_count": len(self._missions),
            "dispatched": sum(1 for m in self._missions.values() if m.status == "dispatched"),
            "completed": sum(1 for m in self._missions.values() if m.status == "completed"),
        }


_DEFAULT_AGV_FLEET: Optional[AGVFleetManager] = None


def get_agv_fleet() -> AGVFleetManager:
    global _DEFAULT_AGV_FLEET
    if _DEFAULT_AGV_FLEET is None:
        _DEFAULT_AGV_FLEET = AGVFleetManager()
    return _DEFAULT_AGV_FLEET
