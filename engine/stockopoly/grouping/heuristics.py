"""Tier-2 offline grouping heuristics — no network, no image decoding.

Signals available without seeing pixel content:

* capture-time proximity (EXIF / manifest timestamps)
* GPS proximity (haversine)
* perceptual-hash similarity (dhash hamming, when intake had Pillow)
* bundle structure — an orbit/facade capture session *is* one object
* filename hints ("ref", "ruler", "scale", "tape") → scale_reference

Pairs of photos are scored, edges above the confidence floor feed a
union-find, and each component ≥2 photos becomes a proposed
``same_object`` group.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

from ..intake.hashes import hamming

_REF_HINT = re.compile(r"(?:^|[_\-./])(ref|ruler|scale|tape)(?:[_\-.0-9]|$)", re.I)

# Presets whose whole bundle is by construction a single subject
_SINGLE_SUBJECT_PRESETS = {"orbit_object", "orbit", "facade", "stockpile"}


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    rad = math.pi / 180.0
    dlat = (lat2 - lat1) * rad
    dlng = (lng2 - lng1) * rad
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin(dlng / 2) ** 2)
    return 6371000.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _pair_score(a: dict, b: dict, *, time_gap_s: float, gps_radius_m: float,
                dhash_max: int) -> float:
    score = 0.0
    ta, tb = _parse_ts(a.get("captured_at")), _parse_ts(b.get("captured_at"))
    if ta is not None and tb is not None and abs(ta - tb) <= time_gap_s:
        score += 0.5
    if None not in (a.get("lat"), a.get("lng"), b.get("lat"), b.get("lng")):
        if haversine_m(a["lat"], a["lng"], b["lat"], b["lng"]) <= gps_radius_m:
            score += 0.25
    dist = hamming(a.get("dhash"), b.get("dhash"))
    if dist is not None and dist <= dhash_max:
        score += 0.35
    return min(score, 0.95)


class _UnionFind:
    def __init__(self, keys: list[str]):
        self.parent = {k: k for k in keys}

    def find(self, k: str) -> str:
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def propose(photos: list[dict], batch: dict, cfg: dict) -> list[dict[str, Any]]:
    """Return group proposals: {kind, label, confidence, photo_ids, meta}."""
    proposals: list[dict[str, Any]] = []

    # Bundle structure: a single-subject preset groups the whole batch.
    manifest = {}
    if batch.get("manifest_json"):
        try:
            manifest = json.loads(batch["manifest_json"])
        except json.JSONDecodeError:
            manifest = {}
    preset = ((manifest.get("session") or {}).get("preset") or "").lower()
    if batch.get("kind") == "bundle" and preset in _SINGLE_SUBJECT_PRESETS and len(photos) >= 2:
        name = (manifest.get("session") or {}).get("name") or batch["batch_id"][:8]
        proposals.append({
            "kind": "same_object",
            "label": name,
            "confidence": 0.9,
            "photo_ids": [p["photo_id"] for p in photos],
            "meta": {"signal": "bundle_preset", "preset": preset},
        })

    # Filename hints → scale-reference candidates.
    for p in photos:
        if _REF_HINT.search(p.get("file") or ""):
            proposals.append({
                "kind": "scale_reference",
                "label": f"Reference: {p['file']}",
                "confidence": 0.5,
                "photo_ids": [p["photo_id"]],
                "meta": {"signal": "filename_hint"},
            })

    # Pairwise time/GPS/dhash clustering (loose batches, or bundles without
    # a single-subject preset).
    if not any(pr["meta"].get("signal") == "bundle_preset" for pr in proposals):
        floor = float(cfg.get("group_confidence_floor", 0.55))
        uf = _UnionFind([p["photo_id"] for p in photos])
        edge_scores: dict[tuple[str, str], float] = {}
        for i in range(len(photos)):
            for j in range(i + 1, len(photos)):
                s = _pair_score(
                    photos[i], photos[j],
                    time_gap_s=float(cfg.get("group_time_gap_s", 45.0)),
                    gps_radius_m=float(cfg.get("group_gps_radius_m", 7.5)),
                    dhash_max=int(cfg.get("group_dhash_max", 10)),
                )
                if s >= floor:
                    uf.union(photos[i]["photo_id"], photos[j]["photo_id"])
                    edge_scores[(photos[i]["photo_id"], photos[j]["photo_id"])] = s

        clusters: dict[str, list[str]] = {}
        for p in photos:
            clusters.setdefault(uf.find(p["photo_id"]), []).append(p["photo_id"])
        n = 0
        for members in clusters.values():
            if len(members) < 2:
                continue
            n += 1
            in_cluster = [s for (a, b), s in edge_scores.items()
                          if a in members and b in members]
            conf = round(sum(in_cluster) / len(in_cluster), 3) if in_cluster else floor
            proposals.append({
                "kind": "same_object",
                "label": f"Cluster {n}",
                "confidence": conf,
                "photo_ids": sorted(members),
                "meta": {"signal": "time_gps_dhash", "edges": len(in_cluster)},
            })
    return proposals
