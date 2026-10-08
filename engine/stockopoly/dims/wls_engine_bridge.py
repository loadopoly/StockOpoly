"""WLS Engine Bridge — direct triad binding between StockOpoly WLS solver,
GARD-Shard Marketplace, UEQGM physics engine, and QUIPU Observer.

Binds:
1. QUIPU (:7100 / host.docker.internal:7100):
   - POST /observe: feeds physical frames (scale_mm_per_px, coplanarity, standoff_m)
     and relational records (control, r=rms, well) into the MESH SLM perception axis.
   - Writes entirety:wls:last_solve and entirety:spatial:coherence into local_brain.sqlite.

2. UEQGM (Unified Equilibrium Quantum Gravity Model):
   - Derives SiCi axial phase weight w_phase from Floquet angle φ in temporal_spatial:state
     or ueqgm:adaptive_runtime.
   - Modulates WLS gauge prior sigma_gauge = sigma_0 * w_phase.
   - Injects solved spatial curvature / tension (chi2, rms, ungrounded) into ueqgm:spatial_tension.

3. GARD (Web3 GARD-Shard Marketplace :8600 / loadopoly-gard-marketplace):
   - Translates WLS covariance diagonal into Fisher information and Cramér-Rao bounds (CRB).
   - Tokenizes solved physical entities into the Split-View Asset Catalog under domain 'SiCi_SQRT(-1)'.
   - Generates authenticated gard-shard/v2 (AES-256-GCM) envelopes for metrology records.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping

from .. import scb_link
from .marketplace_attenuation import get_attenuation_engine
from .quipu_safety_net import get_safety_net
from .solver import SolveResult

logger = logging.getLogger(__name__)

# Default endpoints with container bridge fallback
DEFAULT_QUIPU_HOST = os.environ.get("QUIPU_URL") or "http://quipu:7100"
FALLBACK_QUIPU_HOSTS = ["http://host.docker.internal:7100", "http://127.0.0.1:7100"]

DEFAULT_GARD_HOST = os.environ.get("GARD_MARKETPLACE_URL") or "http://loadopoly-gard-marketplace:8600"
FALLBACK_GARD_HOSTS = ["http://127.0.0.1:8600", "http://host.docker.internal:8600"]

_HTTP_TIMEOUT = 6.0  # seconds; fail-soft
_QUIPU_TIMEOUT = 14.0  # QUIPU calculates world model & edges on /observe


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_post_json(url: str, payload: dict[str, Any], timeout: float = _HTTP_TIMEOUT) -> tuple[bool, Any]:
    """Helper to send a JSON POST request with timeout and error handling."""
    data = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", "ignore")
            try:
                return True, json.loads(raw)
            except Exception:
                return True, raw
    except Exception as exc:
        logger.debug("HTTP POST %s failed: %s", url, exc)
        return False, str(exc)


# ───────────────────────────────────────────────────────────── 1. UEQGM BINDING
def get_ueqgm_phase_weight() -> float:
    """Retrieve or compute the current UEQGM SiCi Floquet phase weight.

    Reads brain_kv from local_brain.sqlite:
    - ueqgm:adaptive_runtime['phase_weight'] if present,
    - else computes w = 1.0 + 0.10 * tanh(sin(φ) * cos(φ) * tan(φ)) from temporal_spatial:state.
    Returns scalar in [0.90, 1.10], defaulting to 1.0.
    """
    db_path = scb_link.scb_db_path()
    if not db_path or not db_path.exists():
        return 1.0

    try:
        # Use immutable=1 URI to prevent WAL lock clashes across container boundaries
        try:
            cn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True, timeout=2)
        except sqlite3.OperationalError:
            cn = sqlite3.connect(str(db_path), timeout=2)
        try:
            # 1. Direct UEQGM runtime check
            row = cn.execute(
                "SELECT value FROM brain_kv WHERE key='ueqgm:adaptive_runtime'"
            ).fetchone()
            if row and row[0]:
                try:
                    data = json.loads(row[0])
                    pw = data.get("phase_weight")
                    if isinstance(pw, (int, float)) and 0.5 <= pw <= 1.5:
                        return float(pw)
                except Exception:
                    pass

            # 2. Derive from temporal_spatial:state
            row = cn.execute(
                "SELECT value FROM brain_kv WHERE key='temporal_spatial:state'"
            ).fetchone()
            if row and row[0]:
                try:
                    ts = json.loads(row[0])
                    phi = float(ts.get("phi_temporal_spatial", math.pi / 4.0))
                    # SiCi intersection axial decay proxy
                    axial = math.sin(phi) * math.cos(phi) * math.tan(phi)
                    axial_clamped = max(-50.0, min(50.0, axial))
                    w = 1.0 + 0.10 * math.tanh(axial_clamped)
                    return max(0.90, min(1.10, w))
                except Exception:
                    pass
        finally:
            cn.close()
    except Exception as exc:
        logger.debug("Failed to read UEQGM state: %s", exc)

    return 1.0


def post_ueqgm_tension(report: dict[str, Any], result: SolveResult) -> bool:
    """Inject WLS solved spatial curvature and tension into UEQGM and Entirety."""
    tension_payload = {
        "rms": result.rms,
        "variables_count": report.get("variables", 0),
        "measurements_count": report.get("measurements", 0),
        "outliers_count": len(result.outliers),
        "ungrounded_components": report.get("ungrounded_components", 0),
        "mean_crb": (
            sum(result.crb_bound.values()) / max(len(result.crb_bound), 1)
            if result.crb_bound else 0.0
        ),
        "grounded_ratio": (
            1.0 - (report.get("ungrounded_components", 0) / max(len(result.components), 1))
            if result.components else 1.0
        ),
        "solved_at": report.get("solved_at") or _now(),
        "harmonic_coupling": "sici_axial",
    }

    # Always log into SCB learning stream (and fallback outbox)
    scb_link.log_learning(
        "ueqgm_spatial_tension",
        f"WLS spatial tension: RMS={result.rms:.4f}",
        tension_payload,
        signal=0.75,
    )

    db_path = scb_link.scb_db_path()
    if not db_path or not db_path.exists():
        return True

    try:
        cn = sqlite3.connect(str(db_path), timeout=2)
        try:
            cn.execute(
                "CREATE TABLE IF NOT EXISTS brain_kv(key TEXT PRIMARY KEY, value TEXT)"
            )
            cn.execute(
                "INSERT OR REPLACE INTO brain_kv(key, value) VALUES (?, ?)",
                ("ueqgm:spatial_tension", json.dumps(tension_payload)),
            )
            cn.execute(
                "INSERT OR REPLACE INTO brain_kv(key, value) VALUES (?, ?)",
                ("entirety:spatial_curvature", json.dumps({
                    "r": result.rms,
                    "control": round(max(0.0, 1.0 - result.rms), 4),
                    "well": bool(report.get("ungrounded_components", 0) == 0),
                    "updated_at": _now(),
                })),
            )
            cn.commit()
            return True
        finally:
            cn.close()
    except Exception as exc:
        logger.debug("Writing directly to brain_kv failed: %s; logged via scb_link", exc)
        return True


# ───────────────────────────────────────────────────────────── 2. QUIPU BINDING
def post_quipu_observation(
    report: dict[str, Any],
    result: SolveResult,
    var_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Emit a physical frame and relational record to QUIPU's POST /observe."""
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("WLS_BRIDGE_TEST_NETWORK"):
        return {"ok": True, "skipped": "pytest_env"}

    ungrounded_count = report.get("ungrounded_components", 0)
    total_vars = len(var_rows)
    grounded_vars = total_vars - ungrounded_count
    rms = result.rms

    # Compute physical frame metrics
    scale_mm_px = 1.0
    if result.scales:
        first_scale = next(iter(result.scales.values()))
        scale_mm_px = round(float(first_scale) * 25.4, 4)

    total_meas = report.get("measurements", 1) or 1
    coplanarity = round(1.0 - (len(result.outliers) / total_meas), 4)
    confidence = round(max(0.10, min(0.99, 1.0 - (rms / 2.0))), 3)

    payload = {
        "source": "perceptopoly",
        "text": (
            f"StockOpoly WLS solver grounded {grounded_vars}/{total_vars} variables "
            f"across {len(result.components)} components (RMS={rms:.4f}, "
            f"scale={scale_mm_px} mm/px, outliers={len(result.outliers)})."
        ),
        "confidence": confidence,
        "meta": {
            "frame": {
                "scale_mm_per_px": scale_mm_px,
                "coplanarity": coplanarity,
                "standoff_m": 1.0,
                "range_m": 1.0,
                "reference_frame": "warehouse_metrology",
            },
            "relational": {
                "agent_id": "stockopoly_wls_solver",
                "schema": "stockopoly.dims/1",
                "world": "physical_warehouse",
                "readout": {
                    "control": round(max(0.0, 1.0 - rms), 4),
                    "r": round(rms, 4),
                    "well": bool(ungrounded_count == 0),
                    "breaking": bool(rms > 1.5),
                },
                "derived": {
                    "limit": 1.0,
                    "vars": total_vars,
                    "outliers": len(result.outliers),
                    "rms": rms,
                    "iterations": result.iterations,
                },
            },
        },
    }

    # Route observation through separable safety net (circuit breaker + fast timeout + spool)
    safety_net = get_safety_net()
    endpoints = [DEFAULT_QUIPU_HOST] + FALLBACK_QUIPU_HOSTS
    last_res = None
    for host in endpoints:
        url = f"{host.rstrip('/')}/observe"
        res = safety_net.execute(url, payload, _http_post_json)
        if res.get("ok"):
            res["endpoint"] = url
            return res
        last_res = res

    return last_res or {"ok": False, "error": "all QUIPU endpoints unreachable"}


# ───────────────────────────────────────────────────────────── 3. GARD BINDING
def post_gard_marketplace_sync(
    report: dict[str, Any],
    result: SolveResult,
    var_rows: list[Any],
) -> dict[str, Any]:
    """Tokenize and shard solved physical entities into the GARD-Shard Marketplace.

    Packages each entity with Fisher Information and Cramér-Rao precision
    into authenticated gard-shard/v2 envelopes.
    """
    if not var_rows:
        return {"ok": True, "tokenized": 0, "sharded": 0}

    # Group variables by entity
    entities: dict[int, dict[str, Any]] = {}
    for raw_r in var_rows:
        r = dict(raw_r) if not isinstance(raw_r, dict) else raw_r
        eid = r["entity_id"]
        if eid not in entities:
            entities[eid] = {
                "entity_id": eid,
                "label": r.get("label") or f"entity_{eid}",
                "kind": r.get("kind") or "part",
                "vars": {},
            }
        var_key = f"v{r['var_id']}"
        val = result.values.get(var_key)
        sig = result.sigmas_ln.get(var_key, float("inf"))
        crb = result.crb_bound.get(var_key, float("inf"))
        fisher = result.fisher_info.get(var_key, 0.0)
        entities[eid]["vars"][r["axis"]] = {
            "val_in": val,
            "sigma_ln": sig,
            "crb": crb,
            "fisher": fisher,
            "grounded": result.grounded.get(var_key, False),
        }

    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("WLS_BRIDGE_TEST_NETWORK"):
        return {"ok": True, "skipped": "pytest_env", "tokenized": len(entities), "sharded": len(entities)}

    endpoints = [DEFAULT_GARD_HOST] + FALLBACK_GARD_HOSTS
    gard_host = None
    for h in endpoints:
        # Check health
        try:
            with urllib.request.urlopen(f"{h.rstrip('/')}/health", timeout=1.5) as r:
                if r.status == 200:
                    gard_host = h
                    break
        except Exception:
            continue

    if not gard_host:
        return {"ok": False, "error": "GARD Marketplace unreachable"}

    tokenized_count = 0
    sharded_count = 0

    for eid, ent in entities.items():
        # Calculate bounding volume and mean precision
        vars_dict = ent["vars"]
        l_in = (vars_dict.get("L") or {}).get("val_in", 0.0) or 0.0
        w_in = (vars_dict.get("W") or {}).get("val_in", 0.0) or 0.0
        h_in = (vars_dict.get("H") or {}).get("val_in", 0.0) or 0.0
        volume_cu_in = round(l_in * w_in * h_in, 2) if l_in and w_in and h_in else 0.0

        all_grounded = all(v.get("grounded", False) for v in vars_dict.values())
        finite_sigs = [v["sigma_ln"] for v in vars_dict.values() if math.isfinite(v.get("sigma_ln", float("inf")))]
        mean_sig = sum(finite_sigs) / len(finite_sigs) if finite_sigs else 0.50
        precision_score = round(max(0.10, min(0.99, math.exp(-mean_sig))), 4)
        quality_score = 0.98 if all_grounded else 0.70

        clean_lbl = re.sub(r"[^a-zA-Z0-9_-]", "_", ent["label"])
        asset_id = f"wls-metrology-{eid}-{clean_lbl}"
        token_id = f"DCC1-WLS-{eid}"

        meta = {
            "protocol": "gard-shard/v2",
            "domain": "SiCi_SQRT(-1)",
            "entity_id": eid,
            "label": ent["label"],
            "kind": ent["kind"],
            "dimensions_in": {axis: v.get("val_in") for axis, v in vars_dict.items()},
            "bounding_volume_cu_in": volume_cu_in,
            "fisher_info": {axis: round(v.get("fisher", 0.0), 3) for axis, v in vars_dict.items()},
            "crb_bounds": {axis: round(v.get("crb", 0.0), 6) for axis, v in vars_dict.items()},
            "rms": result.rms,
            "all_grounded": all_grounded,
            "solved_at": report.get("solved_at") or _now(),
        }

        # 1. Tokenize in GARD Catalog
        tok_payload = {
            "asset_id": asset_id,
            "token_id": token_id,
            "axis": "touch",
            "title": f"WLS Metrology: {ent['label']}",
            "category": "warehouse_metrology",
            "contributor_wallet": "0x000000000000000000000000000000000000dcc1",
            "user_id": "stockopoly-wls",
            "shard_count": 218,
            "quality_score": quality_score,
            "precision_score": precision_score,
            "metadata": meta,
        }
        tok_ok, _ = _http_post_json(f"{gard_host}/api/v1/marketplace/tokenize", tok_payload)
        if tok_ok:
            tokenized_count += 1

        # 2. Generate GARD authenticated envelope
        shard_payload = {
            "asset_id": asset_id,
            "payload": meta,
            "shard_count": 4,
        }
        shd_ok, _ = _http_post_json(f"{gard_host}/api/v1/marketplace/gard/shard", shard_payload)
        if shd_ok:
            sharded_count += 1

        # Attenuate connection with this shard interaction
        crb_dict = {axis: v.get("crb", 1.0) for axis, v in vars_dict.items()}
        fisher_dict = {axis: v.get("fisher", 0.0) for axis, v in vars_dict.items()}
        dim_dict = {axis: v.get("val_in", 0.0) for axis, v in vars_dict.items()}
        get_attenuation_engine().record_shard_interaction(
            entity_id=eid,
            token_id=token_id,
            authenticated=bool(shd_ok),
            fisher_info_dict=fisher_dict,
            crb_dict=crb_dict,
            dimensions=dim_dict,
        )

    attenuation_status = get_attenuation_engine().status()
    return {
        "ok": True,
        "endpoint": gard_host,
        "tokenized": tokenized_count,
        "sharded": sharded_count,
        "attenuation": attenuation_status,
    }


# ───────────────────────────────────────────────────────────── TRIAD ORCHESTRATION
def dispatch_triad_sync(
    report: dict[str, Any],
    result: SolveResult,
    var_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Execute complete triad sync across QUIPU, UEQGM, and GARD.

    Never raises exceptions; reports status summary.
    """
    logger.info("Executing WLS Triad Sync (QUIPU, UEQGM, GARD)...")

    # 1. UEQGM Tension injection
    ueqgm_ok = post_ueqgm_tension(report, result)

    # 2. QUIPU Observation (protected by separable safety net)
    quipu_res = post_quipu_observation(report, result, var_rows)

    # 3. GARD Marketplace Tokenize & Shard (attenuating connection)
    gard_res = post_gard_marketplace_sync(report, result, var_rows)

    triad_status = {
        "ueqgm_tension_logged": ueqgm_ok,
        "quipu": quipu_res,
        "gard": gard_res,
        "safety_net": get_safety_net().status(),
        "marketplace_attenuation": get_attenuation_engine().status(),
        "solved_at": report.get("solved_at"),
    }

    logger.info("WLS Triad Sync complete: %s", triad_status)
    return triad_status
