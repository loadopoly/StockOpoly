"""Cyber-Physical Cognitive Pathway Router & Efferent Directive Bridge.

Deterministically separates HolonHubLive realizations into:
  - Systemic Pathways (R2, R3, R4, R5, R7, R10): ERP/master-data configuration & procurement signals.
  - Operational Directives: SHA-1 deduplicated efferent work orders (`body_directives` / `body_feedback`)
    routed across Floor Kanban, Child Hubs, and M365 Copilot / Teams / SharePoint surfaces.

No external dependencies (stdlib only) so it runs inside both HubCore air-lock python services
and portable HDS suite distributions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SYSTEMIC_RULES: Dict[str, Dict[str, Any]] = {
    "R7": {
        "ruleId": "R7",
        "name": "Populate ABC Inventory Catalog",
        "sapSignalType": "PR_ADJUST",
        "mechanism": "Run ABC classification job against active items; write-back to PFEP master.",
        "downstreamLift": "Drives cycle-count cadence and unlocks multi-echelon safety-stock math.",
        "owner": "IT Governance + Planner",
        "sequence": 1,
    },
    "R3": {
        "ruleId": "R3",
        "name": "Switch Safety Stock Planning Method",
        "sapSignalType": "PO_ADJUST",
        "mechanism": "Set non-zero Safety Stock + assign Safety Stock Planning Method for recoverable parts.",
        "downstreamLift": "Converts recoverable stockouts (2-sigma@10d / 3-sigma@6d) into on-time fills.",
        "owner": "Planner + Buyer",
        "sequence": 2,
    },
    "R5": {
        "ruleId": "R5",
        "name": "Transaction Reason Code enforcement",
        "sapSignalType": "PR_ADJUST",
        "mechanism": "Enforce structured reason codes on cycle-count & floor damage adjustments.",
        "downstreamLift": "Makes physical WIP/door variance attributable for self-annealing elimination.",
        "owner": "IT Governance + Quality",
        "sequence": 3,
    },
    "R2": {
        "ruleId": "R2",
        "name": "Allocation reconciliation batch job",
        "sapSignalType": "PO_ADJUST",
        "mechanism": "Reconcile orphaned reservations vs physical floor QOH & canceled orders.",
        "downstreamLift": "Collapses allocation-gap stockouts and receiving dock holds without capital spend.",
        "owner": "IT Governance + Planner",
        "sequence": 4,
    },
    "R10": {
        "ruleId": "R10",
        "name": "Assign Buyer / Planner codes",
        "sapSignalType": "RDD_CREDIT_SHIFT",
        "mechanism": "Backfill Buyer Name / Planner ownership and scorecard routing on active SKUs.",
        "downstreamLift": "Deterministic vendor expedite & CTO SLA accountability.",
        "owner": "IT Governance + Procurement",
        "sequence": 5,
    },
    "R4": {
        "ruleId": "R4",
        "name": "Item Cycle Count Enabled audit",
        "sapSignalType": "PR_ADJUST",
        "mechanism": "Audit & reconcile Stockopoly bin geometry against SAP Storage Locations.",
        "downstreamLift": "Restores bin-level physical accuracy across 3D slotting and floor picks.",
        "owner": "Site Lead + IT Governance",
        "sequence": 6,
    },
}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _severity(priority: float) -> str:
    if priority >= 0.85:
        return "critical"
    if priority >= 0.65:
        return "act"
    if priority >= 0.40:
        return "watch"
    return "info"


def _m365_dispatch_mode(priority: float) -> str:
    if priority >= 0.85:
        return "IMMEDIATE_CARD"
    if priority >= 0.65:
        return "SHIFT_DIGEST"
    return "LOG_ONLY"


def _fingerprint(source: str, signal_kind: str, target_entity: str, title: str) -> str:
    h = hashlib.sha1()
    h.update(str(source or "").encode("utf-8"))
    h.update(b"|")
    h.update(str(signal_kind or "").encode("utf-8"))
    h.update(b"|")
    h.update(str(target_entity or "").encode("utf-8"))
    h.update(b"|")
    h.update(str(title or "").encode("utf-8"))
    return h.hexdigest()


class CyberPhysicalPathwayRouter:
    """Routes physical-digital telemetry through systemic levers and deduplicated efferent directives."""

    RETURN_CHAIN_OWNER = "return-quarantine-governance"

    @classmethod
    def classify_and_route(
        cls,
        relational_state: Dict[str, Any],
        *,
        existing_directives: Optional[List[Dict[str, Any]]] = None,
        trigger_action: str = "",
    ) -> Dict[str, Any]:
        rel = relational_state or {}
        fired_systemic: Dict[str, Dict[str, Any]] = {}
        fresh_ops: List[Dict[str, Any]] = []
        now = _utc_now()

        def add_systemic(rule_id: str, evidence: str, target_ref: str) -> None:
            tmpl = SYSTEMIC_RULES.get(rule_id)
            if not tmpl:
                return
            entry = fired_systemic.get(rule_id)
            if entry is None:
                entry = {
                    **tmpl,
                    "blockedRawInventoryAdjust": True,
                    "evidenceRefs": [],
                    "evidenceNotes": [],
                }
                fired_systemic[rule_id] = entry
            if target_ref and target_ref not in entry["evidenceRefs"]:
                entry["evidenceRefs"].append(target_ref)
            if evidence and len(entry["evidenceNotes"]) < 6:
                entry["evidenceNotes"].append(evidence)

        def build_directive(
            *,
            signal_kind: str,
            priority: float,
            owner_role: str,
            target_entity: str,
            title: str,
            do_this: str,
            why_it_matters: str,
            kanban_lane: str,
            chain: str = "Fulfill",
            related_refs: Optional[List[str]] = None,
            evidence: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            source = "holonhub-live"
            fp = _fingerprint(source, signal_kind, target_entity, title)
            sev = _severity(priority)
            mode = _m365_dispatch_mode(priority)
            refs = [str(r).strip().upper() for r in (related_refs or []) if str(r or "").strip()]
            return {
                "directiveId": f"DIR-{fp[:10].upper()}",
                "fingerprint": fp,
                "source": source,
                "signalKind": signal_kind,
                "priority": round(float(priority), 4),
                "severity": sev,
                "chain": chain,
                "ownerRole": owner_role,
                "targetEntity": target_entity,
                "relatedRefs": refs,
                "kanbanLane": kanban_lane,
                "m365DispatchMode": mode,
                "title": title,
                "doThis": do_this,
                "whyItMatters": why_it_matters,
                "evidence": evidence or {},
                "status": "open",
                "createdAt": now,
                "lastStatusAt": now,
                "m365CardEnvelope": {
                    "dispatchMode": mode,
                    "toolTarget": "teams_post_handoff_card" if mode == "IMMEDIATE_CARD" else "sp_write_artifact",
                    "governor": "verifier-governor",
                    "credentialPolicy": "ENV/VAULT/SECRETREF-ONLY",
                    "title": title,
                    "ownerRole": owner_role,
                    "severity": sev,
                    "priority": round(float(priority), 4),
                } if mode != "LOG_ONLY" else None,
            }

        # 1. Probabilistic Stockouts, Kit Blocks & Consolidation Realizations
        ev = rel.get("lastHeuristicsEvaluation") or {}
        qid = str(ev.get("quoteId") or rel.get("selectedQuoteId") or "ROF-DEMO-001")
        local_dc = str(ev.get("localDc") or "NC34")
        kitting_dc = str(ev.get("kittingDc") or "GA02")
        for ld in ev.get("lineDecisions") or []:
            sku = str(ld.get("sku") or "").upper()
            spoke = str(ld.get("spokeDc") or local_dc).upper()
            dcode = str(ld.get("decisionCode") or "")
            s3 = float(ld.get("sigma3At6dPct") or 0.0)
            s2 = float(ld.get("sigma2At10dPct") or 0.0)
            ten_day = bool(ld.get("tenDayAlertTriggered", False))
            if s3 >= 0.27 or dcode == "PEEL_BACKORDER":
                add_systemic("R3", f"SKU {sku} breached 3-sigma@6d ({s3}%) on {qid}", f"{qid}:{sku}")
                add_systemic("R10", f"Assign expedite owner for {sku} ({spoke}->{kitting_dc})", sku)
                fresh_ops.append(
                    build_directive(
                        signal_kind="stockout_3sigma_6d",
                        priority=0.95,
                        owner_role="Buyer / CSR",
                        target_entity=f"SKU::{sku}::{qid}",
                        title=f"Critical 3-sigma@6d stockout risk on {sku} ({s3}%) for {qid}",
                        do_this=f"Trigger expedite PO / spoke transfer ({spoke}->{kitting_dc}) and send 5d CSR warning.",
                        why_it_matters="Exceeds 0.27% 3-sigma@6d stockout guardrail on active kitting wave.",
                        kanban_lane="TO_PICK",
                        chain="Order",
                        related_refs=[qid, sku, spoke, kitting_dc],
                        evidence={"sigma3At6dPct": s3, "sigma2At10dPct": s2, "decisionCode": dcode},
                    )
                )
            elif ten_day:
                add_systemic("R7", f"2-sigma@10d early warning on {sku} ({s2}%) for {qid}", f"{qid}:{sku}")
                fresh_ops.append(
                    build_directive(
                        signal_kind="stockout_2sigma_10d",
                        priority=0.80,
                        owner_role="Buyer / Planner",
                        target_entity=f"SKU::{sku}::{qid}",
                        title=f"10-day 2-sigma watch on {sku} ({s2}%) at {kitting_dc}",
                        do_this=f"Review IFR replenishment & 5-day CSR window before VL01N wave drop on {qid}.",
                        why_it_matters="Early Day-10 probability buffer alert allows planned procurement before expedite.",
                        kanban_lane="TO_PICK",
                        chain="Plan",
                        related_refs=[qid, sku, spoke, kitting_dc],
                        evidence={"sigma2At10dPct": s2, "decisionCode": dcode},
                    )
                )
            elif dcode == "WAIT_FOR_SUPPLY_PRESERVE_KIT":
                fresh_ops.append(
                    build_directive(
                        signal_kind="preserve_future_kit",
                        priority=0.55,
                        owner_role="Ops / Planner",
                        target_entity=f"KIT::{qid}::{sku}",
                        title=f"Preserve future kit {qid}: hold {sku} for scheduled inbound",
                        do_this="Do not strip future kit; recheck automatically at Var2 (RDD-6d).",
                        why_it_matters="Slide Step 8 Golden Rule prevents premature manual kit splitting.",
                        kanban_lane="TO_PICK",
                        chain="Order",
                        related_refs=[qid, sku, kitting_dc],
                        evidence={"decisionCode": dcode},
                    )
                )

        # 2. 7th Chain (Return) — Receiving Quarantine & Cancel-to-PO-Reduction
        for qr in rel.get("receivingQuarantines") or []:
            if str(qr.get("status") or "").upper() == "RECEIVING_QUARANTINE_RETURN":
                qrn_id = str(qr.get("quarantineId") or "QRN-1")
                ord_id = str(qr.get("orderId") or qid)
                sku = str(qr.get("sku") or "").upper()
                add_systemic("R2", f"Reconcile canceled order {ord_id} inbound quarantine {qrn_id}", qrn_id)
                fresh_ops.append(
                    build_directive(
                        signal_kind="receiving_quarantine_return",
                        priority=0.88,
                        owner_role="Receiving / Return Lead",
                        target_entity=f"QRN::{qrn_id}",
                        title=f"Hold {qrn_id} ({sku}) on dock for Vendor Return ({ord_id})",
                        do_this="Segregate in Receiving Quarantine Gate, draft vendor RMA, and verify PO reduction.",
                        why_it_matters="Inbound stock arrived without upstream PO cancellation reduction.",
                        kanban_lane="QUARANTINE_RETURN",
                        chain="Return",
                        related_refs=[qrn_id, ord_id, sku],
                        evidence={"qty": qr.get("qty"), "quarantineAction": qr.get("quarantineAction")},
                    )
                )

        for po in rel.get("autoReplenishmentPos") or []:
            if str(po.get("status") or "").upper() == "PO_REDUCTION_REQUIRED":
                po_id = str(po.get("poId") or "PO-RED")
                sku = str(po.get("sku") or "").upper()
                dc = str(po.get("dc") or "TX03").upper()
                ord_id = str(po.get("orderId") or qid)
                add_systemic("R2", f"Reduce open PO {po_id} for {sku} at {dc}", po_id)
                fresh_ops.append(
                    build_directive(
                        signal_kind="po_reduction_required",
                        priority=0.70,
                        owner_role="Buyer",
                        target_entity=f"PO::{po_id}",
                        title=f"PO Reduction Required on {po_id} ({sku} @ {dc})",
                        do_this="Send bounded PO_ADJUST signal to SAP before inbound dock receipt.",
                        why_it_matters="Prevents stranded overstock when linked customer quote/order cancels.",
                        kanban_lane="AUTO_REPLEN_PO",
                        chain="Order",
                        related_refs=[po_id, ord_id, sku, dc],
                        evidence={"orderQty": po.get("orderQty"), "dc": dc},
                    )
                )

        # 3. Space <-> SAP Bin Drift Reconciliation
        for bm in rel.get("stockopolyBinMappings") or []:
            if str(bm.get("matchStatus") or "").upper() == "RECONCILE_BIN_DRIFT":
                sku = str(bm.get("sku") or "").upper()
                dc = str(bm.get("dc") or "TX03").upper()
                sloc = str(bm.get("sapStorageLocation") or f"{dc}-BIN")
                add_systemic("R4", f"Stockopoly vs SAP Storage Location drift on {sku} @ {dc}", f"{dc}:{sku}")
                fresh_ops.append(
                    build_directive(
                        signal_kind="bin_drift_reconcile",
                        priority=0.72,
                        owner_role="WMS / Floor Lead",
                        target_entity=f"BIN::{dc}::{sku}",
                        title=f"Reconcile Stockopoly bin drift on {sku} @ {dc} ({sloc})",
                        do_this=f"Verify physical bay coordinates in {dc} and scan bin / PICK_COMPLETE_KNOCKOFF.",
                        why_it_matters="3D slotting geometry disagrees with SAP Storage Location metadata.",
                        kanban_lane="TO_PICK",
                        chain="Transform",
                        related_refs=[dc, sku, sloc],
                        evidence={"sapStorageLocation": sloc, "coords": bm.get("stockopolyCoords")},
                    )
                )

        # 4. Vision OCR / QUIPU Handwritten Floor Notes
        for note in rel.get("quipuNoteClassifications") or []:
            if bool(note.get("wipImpactFlag")):
                nid = str(note.get("noteId") or "NOTE-1")
                cls_name = str(note.get("classification") or "WIP_STAGING_PARTIAL")
                sku = str(note.get("sku") or "").upper()
                dc = str(note.get("dc") or "TX03").upper()
                add_systemic("R5", f"Floor note {nid} classified as {cls_name}", nid)
                fresh_ops.append(
                    build_directive(
                        signal_kind=f"quipu_{cls_name.lower()}",
                        priority=0.76,
                        owner_role="Quality / Ops",
                        target_entity=f"NOTE::{nid}",
                        title=f"QUIPU {cls_name} on {sku} @ {dc} ({nid})",
                        do_this="Inspect staged pallet/door condition and confirm physical count disposition.",
                        why_it_matters="Unstructured handwritten floor note indicates WIP aging, damage, or count variance.",
                        kanban_lane="QUARANTINE_RETURN" if cls_name == "DAMAGED_DOOR_HOLD" else "TO_PICK",
                        chain="Source" if cls_name != "DAMAGED_DOOR_HOLD" else "Return",
                        related_refs=[nid, sku, dc],
                        evidence={"classification": cls_name, "riskScore": note.get("cycleCountRiskScore")},
                    )
                )

        # Deduplicate fresh directives by SHA-1 fingerprint (keep highest priority)
        dedup_fresh: Dict[str, Dict[str, Any]] = {}
        for d in sorted(fresh_ops, key=lambda x: -float(x["priority"])):
            fp = d["fingerprint"]
            if fp not in dedup_fresh:
                dedup_fresh[fp] = d

        # Merge with existing directives (preserve open/ack/in_progress/done/rejected history)
        by_fp: Dict[str, Dict[str, Any]] = {}
        for ex in existing_directives or []:
            if isinstance(ex, dict) and ex.get("fingerprint"):
                by_fp[str(ex["fingerprint"])] = dict(ex)

        emitted = 0
        deduped = 0
        for fp, d in dedup_fresh.items():
            prev = by_fp.get(fp)
            if prev is not None:
                if prev.get("status") == "expired":
                    prev["status"] = "open"
                    prev["lastStatusAt"] = now
                deduped += 1
            else:
                by_fp[fp] = d
                emitted += 1

        merged = sorted(by_fp.values(), key=lambda x: (0 if x.get("status") in ("open", "ack", "in_progress") else 1, -float(x.get("priority") or 0.0)))
        if len(merged) > 200:
            merged = merged[:200]

        m365_queue = [
            d["m365CardEnvelope"]
            for d in merged
            if d.get("status") in ("open", "ack", "in_progress") and d.get("m365CardEnvelope")
        ]
        systemic_list = sorted(fired_systemic.values(), key=lambda x: int(x["sequence"]))

        return {
            "evaluatedAt": now,
            "triggerAction": trigger_action,
            "returnChainOwner": cls.RETURN_CHAIN_OWNER,
            "systemic": systemic_list,
            "operational": merged,
            "m365DispatchQueue": m365_queue,
            "counts": {
                "systemic": len(systemic_list),
                "openOperational": sum(1 for d in merged if d.get("status") in ("open", "ack", "in_progress")),
                "closedOperational": sum(1 for d in merged if d.get("status") == "done"),
                "immediateM365Cards": sum(1 for c in m365_queue if c.get("dispatchMode") == "IMMEDIATE_CARD"),
                "shiftDigestItems": sum(1 for c in m365_queue if c.get("dispatchMode") == "SHIFT_DIGEST"),
                "newlyEmitted": emitted,
                "deduped": deduped,
            },
        }

    @classmethod
    def auto_close_directives_from_physical_event(
        cls,
        directives: List[Dict[str, Any]],
        feedback_log: List[Dict[str, Any]],
        *,
        event_kind: str,
        match_tokens: Iterable[str],
        actor: str,
        outcome: str,
    ) -> List[Dict[str, Any]]:
        """Auto-close matching open directives when physical RFID or SAP knockoff confirms execution."""
        tokens = {str(t or "").strip().upper() for t in match_tokens if str(t or "").strip()}
        if not tokens:
            return []
        now = _utc_now()
        closed: List[Dict[str, Any]] = []
        for d in directives:
            if not isinstance(d, dict) or d.get("status") not in ("open", "ack", "in_progress"):
                continue
            target_blob = " ".join(
                [str(d.get("targetEntity") or ""), str(d.get("title") or "")]
                + [str(r) for r in (d.get("relatedRefs") or [])]
            ).upper()
            if any(tok in target_blob for tok in tokens):
                d["status"] = "done"
                d["lastStatusAt"] = now
                d["closedByPhysicalEvent"] = event_kind
                fb = {
                    "feedbackId": f"FB-{len(feedback_log) + len(closed) + 1}",
                    "directiveId": d.get("directiveId"),
                    "fingerprint": d.get("fingerprint"),
                    "status": "done",
                    "triggerEvent": event_kind,
                    "outcome": outcome,
                    "executedBy": actor,
                    "loggedAt": now,
                }
                feedback_log.append(fb)
                closed.append(fb)
        if len(feedback_log) > 400:
            del feedback_log[: len(feedback_log) - 400]
        return closed

    @classmethod
    def sync_to_sqlite_brain(
        cls,
        sqlite_path: Path,
        directives: List[Dict[str, Any]],
        feedback_rows: List[Dict[str, Any]],
    ) -> None:
        """Best-effort WAL mirror into pipeline/local_brain.sqlite body_directives & body_feedback."""
        try:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(sqlite_path), timeout=2.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS body_directives (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at      TEXT NOT NULL,
                        fingerprint     TEXT NOT NULL UNIQUE,
                        source          TEXT NOT NULL,
                        signal_kind     TEXT NOT NULL,
                        priority        REAL NOT NULL,
                        severity        TEXT NOT NULL,
                        title           TEXT NOT NULL,
                        why_it_matters  TEXT,
                        do_this         TEXT,
                        owner_role      TEXT,
                        target_entity   TEXT,
                        evidence_json   TEXT,
                        status          TEXT NOT NULL DEFAULT 'open',
                        last_status_at  TEXT,
                        value_per_year  REAL
                    );
                    CREATE TABLE IF NOT EXISTS body_feedback (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        directive_id    INTEGER NOT NULL,
                        logged_at       TEXT NOT NULL,
                        status          TEXT NOT NULL,
                        outcome         TEXT,
                        executed_by     TEXT
                    );
                    """
                )
                for d in directives:
                    fp = str(d.get("fingerprint") or "")
                    if not fp:
                        continue
                    conn.execute(
                        """
                        INSERT INTO body_directives(
                            created_at, fingerprint, source, signal_kind, priority, severity,
                            title, why_it_matters, do_this, owner_role, target_entity,
                            evidence_json, status, last_status_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(fingerprint) DO UPDATE SET
                            priority=excluded.priority,
                            severity=excluded.severity,
                            status=excluded.status,
                            last_status_at=excluded.last_status_at
                        """,
                        (
                            d.get("createdAt") or _utc_now(),
                            fp,
                            d.get("source") or "holonhub-live",
                            d.get("signalKind") or "operational",
                            float(d.get("priority") or 0.5),
                            d.get("severity") or "info",
                            d.get("title") or "Directive",
                            d.get("whyItMatters") or "",
                            d.get("doThis") or "",
                            d.get("ownerRole") or "Ops",
                            d.get("targetEntity") or "",
                            json.dumps(d.get("evidence") or {}, default=str),
                            d.get("status") or "open",
                            d.get("lastStatusAt") or _utc_now(),
                        ),
                    )
                for fb in feedback_rows:
                    fp = str(fb.get("fingerprint") or "")
                    row_id = None
                    if fp:
                        row = conn.execute(
                            "SELECT id FROM body_directives WHERE fingerprint=?",
                            (fp,),
                        ).fetchone()
                        if row:
                            row_id = int(row[0])
                    if row_id is not None:
                        conn.execute(
                            "INSERT INTO body_feedback(directive_id, logged_at, status, outcome, executed_by) VALUES(?,?,?,?,?)",
                            (
                                row_id,
                                fb.get("loggedAt") or _utc_now(),
                                fb.get("status") or "done",
                                fb.get("outcome") or "",
                                fb.get("executedBy") or "system",
                            ),
                        )
                conn.commit()
        except Exception:
            pass
