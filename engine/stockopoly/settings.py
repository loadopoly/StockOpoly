"""Typed app settings with defaults, persisted in the ``settings`` table.

Values are JSON-encoded so booleans/numbers/lists round-trip. ``DEFAULTS``
is the single source of truth for keys and their types; unknown keys are
rejected to catch typos early.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .store import open_conn

DEFAULTS: dict[str, Any] = {
    # Location label grammar: named groups aisle/bay/level/bin (bin optional).
    "location_grammar": r"^(?P<aisle>[A-Z]{1,2})-?(?P<bay>\d{1,3})-?(?P<level>\d{1,2})(?:-?(?P<bin>[A-Z0-9]{1,3}))?$",
    # Slotting / occupancy
    "over_capacity_threshold": 1.0,   # occupancy fraction considered "over" (5d)
    "fill_factor": 0.85,
    "daily_move_budget": 25,          # moves per day in migration plans (6b)
    "golden_zone_levels": [1, 2],     # pick-friendly levels reserved for top velocity
    "dock_code": "DOCK",
    # Default physical assumptions when the solver has no grounded dims yet
    "default_bay_width_in": 96.0,
    "default_rack_depth_in": 42.0,
    "default_level_height_in": 60.0,
    "default_unit_volume_in3": 64.0,  # parts with no dims: assume 4x4x4
    "default_lead_time_days": 14.0,
    # Sharing + integration toggles
    "share_supabase": True,           # cleanly no-ops until SUPABASE_URL/key env exist;
                                      # the OFF switch fully disables (user requirement)
    "crawl_live": True,               # live supplier crawl on explicit user action only
    "scb_vision": False,              # grouping tier 1 (SCB-native) opt-in
    "llm_vision": False,              # grouping tier 3 (OpenRouter/Grok) opt-in
    # Grouping cascade
    "group_confidence_floor": 0.55,
    "group_time_gap_s": 45.0,         # tier-2: same-object if shot within this gap
    "group_gps_radius_m": 7.5,        # tier-2: and/or within this radius
    "group_dhash_max": 10,            # tier-2: hamming distance for visual match
    "llm_vision_model": "x-ai/grok-2-vision-1212",  # tier-3 via OpenRouter
    # Supplier crawl seeds: list of {"supplier": str, "url_pattern": str} where
    # url_pattern may contain {part} / {supplier_part}
    "supplier_sites": [],
    # Safety stock default scenario
    "ss_scenario": "baseline",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(key: str, cn: sqlite3.Connection | None = None) -> Any:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    own = cn is None
    if own:
        cn = open_conn()
    try:
        row = cn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    finally:
        if own:
            cn.close()
    if row is None:
        return DEFAULTS[key]
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return DEFAULTS[key]


def put(key: str, value: Any, cn: sqlite3.Connection | None = None) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    own = cn is None
    if own:
        cn = open_conn()
    try:
        cn.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), _now()),
        )
        cn.commit()
    finally:
        if own:
            cn.close()


def all_settings(cn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = cn is None
    if own:
        cn = open_conn()
    try:
        rows = cn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        if own:
            cn.close()
    merged = dict(DEFAULTS)
    for r in rows:
        if r["key"] in DEFAULTS:
            try:
                merged[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                pass
    return merged


def update(values: dict[str, Any]) -> dict[str, Any]:
    """Bulk update from the API; ignores unknown keys, returns merged settings."""
    cn = open_conn()
    try:
        for k, v in values.items():
            if k in DEFAULTS:
                put(k, v, cn=cn)
        return all_settings(cn=cn)
    finally:
        cn.close()
