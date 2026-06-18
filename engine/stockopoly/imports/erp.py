"""Optional ERP bridge — rides the Supply-Chain-Brain's data_access layer.

Triple-gated: ``STOCKOPOLY_ERP=1`` env, SCB sibling checkout present, and
its (pandas-based) ``src.brain.data_access`` importable. StockOpoly's core
stays stdlib-only; this module is the one place allowed to *borrow* the
Brain's heavier stack, and only ever at the caller's explicit request.

Usage::

    rows = pull("epicor", "inventory_on_hand")     # list[dict]
    imports.import_inventory(rows)
"""
from __future__ import annotations

import logging
import os
import sys

from ..scb_link import find_scb_repo

logger = logging.getLogger(__name__)


def erp_available() -> dict:
    """Report bridge readiness (the API exposes this verbatim)."""
    enabled = os.environ.get("STOCKOPOLY_ERP", "") == "1"
    repo = find_scb_repo()
    status = {"enabled": enabled, "scb_repo": str(repo) if repo else None,
              "importable": False, "reason": None}
    if not enabled:
        status["reason"] = "set STOCKOPOLY_ERP=1 to enable"
        return status
    if repo is None:
        status["reason"] = "VS-Code workspace (pipeline/) not found — set SCB_REPO_DIR"
        return status
    pipeline = repo / "pipeline"
    sys_path_added = False
    try:
        if str(pipeline) not in sys.path:
            sys.path.insert(0, str(pipeline))
            sys_path_added = True
        import importlib
        importlib.import_module("src.brain.data_access")
        status["importable"] = True
    except Exception as exc:  # pandas missing, config absent, …
        status["reason"] = f"data_access import failed: {exc}"
        if sys_path_added:
            sys.path.remove(str(pipeline))
    return status


def pull(connector: str, logical_name: str) -> list[dict]:
    """Fetch a logical table through SCB connectors → list of dict rows.

    Raises ``RuntimeError`` with the gating reason when unavailable, so the
    API can surface it to the user instead of silently returning nothing.
    """
    status = erp_available()
    if not status["importable"]:
        raise RuntimeError(status["reason"] or "ERP bridge unavailable")
    from src.brain import data_access  # type: ignore

    df = data_access.fetch_logical(connector, logical_name)
    rows = df.to_dict("records")
    logger.info("ERP pull %s/%s → %d rows", connector, logical_name, len(rows))
    return rows
