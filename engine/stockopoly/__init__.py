"""StockOpoly engine — warehouse mapping + slotting from photogrammetry.

Standalone Python engine (stdlib-first) behind the StockOpoly app. Ingests
``loadopoly.capture/1`` photo bundles and loose JPEG batches, groups photos
(vision cascade), solves a relational dimension graph into real-world
dimensions, builds the warehouse location space and 3D map, computes
current-state slotting, and optimizes future-state slotting with a day-by-day
migration task list.

Sibling repos: ``../Loadopoly-OCR`` (capture producer) and
``../Supply-Chain-Brain`` (learning_log consumer via ``scb_link``).
"""
from __future__ import annotations

__version__ = "0.1.0"
API_VERSION = "stockopoly.api/1"
