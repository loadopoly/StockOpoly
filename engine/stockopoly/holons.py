"""Holon Desk backend: lists the holons registered in ``holons/registry.json``.

Read-only. Each entry is enriched with name/version/capabilities from its
manifest when the manifest is present in this repo; holons hosted in another
repo (e.g. ``x_deepmap`` in Perceptopoly) are reported as ``external``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # StockOpoly/
HOLONS_DIR = _REPO_ROOT / "holons"
THIS_REPO = "loadopoly/StockOpoly"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_holons(holons_dir: Path | None = None) -> dict[str, Any]:
    root = holons_dir or HOLONS_DIR
    registry = _load(root / "registry.json") or {}
    out: list[dict[str, Any]] = []
    for entry in registry.get("holons", []):
        row = dict(entry)
        row["external"] = entry.get("hostRepo") != THIS_REPO
        row["capabilities"] = []
        row["version"] = None
        manifest = entry.get("manifest")
        if manifest and not row["external"]:
            doc = _load(root.parent / manifest) or {}
            holon = doc.get("holon", {})
            row["version"] = holon.get("version")
            row["name"] = holon.get("name", row.get("name"))
            row["capabilities"] = [c.get("id") for c in doc.get("capabilities", []) if c.get("id")]
        out.append(row)
    return {"schema": registry.get("schema"), "hub": registry.get("hub"), "holons": out}
