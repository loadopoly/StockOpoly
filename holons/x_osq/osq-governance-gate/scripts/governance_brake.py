#!/usr/bin/env python3
"""Stage 4: OSQ Forecast Governance Gate & Emergency Brake.

Enforces HubCore governance and the Emergency Brake:
1. Automated exception detection.
2. Volume spike evaluation vs historical thresholds.
3. Confidence boundary enforcement.
4. Approval workflow generation for held / high-risk demand.
5. Emits HubCore Task / Event audit envelope. Stdlib only.
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path


def evaluate_governance_brake(quipu_data: dict, mode: str = "adaptive") -> dict:
    lines = quipu_data.get("curatedDemandLines", [])
    batch_id = quipu_data.get("envelope", {}).get("batchId", "BATCH-GOV")

    brake_tripped = False
    brake_reasons = []
    approval_requests = []
    approved_lines = []
    quarantined_lines = []

    # Configurable thresholds based on governance mode
    CONFIDENCE_MIN = 0.40 if mode == "strict" else 0.25 if mode == "adaptive" else 0.10
    MAX_IMPORT_UNITS_WITHOUT_APPROVAL = 500 if mode == "strict" else 1500

    total_curated_units = 0

    for item in lines:
        sku = item["sku"]
        quote_id = item["quoteId"]
        conf = item.get("quipuConfidenceScore", 0.5)
        origin = item.get("origin", "DOMESTIC")
        weekly = item.get("weeklyForecast", [])

        curated_units = sum(w["curatedQuantity"] for w in weekly)
        total_curated_units += curated_units

        # Rule 1: Emergency Brake on severely low confidence
        if conf < CONFIDENCE_MIN:
            brake_tripped = True
            reason = f"EMERGENCY BRAKE: SKU {sku} (Quote {quote_id}) confidence {conf} is below threshold {CONFIDENCE_MIN}."
            brake_reasons.append(reason)
            approval_requests.append({
                "approvalId": f"APPR-{batch_id}-{sku}",
                "sku": sku,
                "quoteId": quote_id,
                "type": "LOW_CONFIDENCE_EXCEPTION",
                "confidence": conf,
                "status": "PENDING_PLANNER_REVIEW",
                "recommendedAction": "Hold demand publication until sales verification."
            })
            item["governanceStatus"] = "HELD"
            quarantined_lines.append(item)
            continue

        # Rule 2: Emergency Brake on large import spike
        if origin == "IMPORT" and curated_units > MAX_IMPORT_UNITS_WITHOUT_APPROVAL:
            brake_tripped = True
            reason = f"EMERGENCY BRAKE: Import SKU {sku} requires {curated_units} units exceeding unapproved cap ({MAX_IMPORT_UNITS_WITHOUT_APPROVAL})."
            brake_reasons.append(reason)
            approval_requests.append({
                "approvalId": f"APPR-{batch_id}-{sku}-IMPORT",
                "sku": sku,
                "quoteId": quote_id,
                "type": "HIGH_VOLUME_IMPORT_EXCEPTION",
                "volume": curated_units,
                "status": "PENDING_SUPPLY_CHAIN_APPROVAL",
                "recommendedAction": "Confirm supplier container allocation before publishing to Blue Yonder."
            })
            item["governanceStatus"] = "HELD"
            quarantined_lines.append(item)
            continue

        item["governanceStatus"] = "APPROVED"
        approved_lines.append(item)

    summary = {
        "batchId": batch_id,
        "governanceMode": mode,
        "emergencyBrakeTripped": brake_tripped,
        "totalLinesEvaluated": len(lines),
        "linesApproved": len(approved_lines),
        "linesHeldForApproval": len(quarantined_lines),
        "totalCuratedUnits": round(total_curated_units, 2),
        "approvalRequestsCount": len(approval_requests)
    }

    envelope = {
        "envelope": {
            "type": "OSQ_GOVERNANCE_DECISION",
            "version": "1.0.0",
            "timestampUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "batchId": batch_id,
            "governanceMode": mode,
            "state": "HELD" if (brake_tripped and len(approved_lines) == 0) else "PARTIALLY_APPROVED" if brake_tripped else "ACCEPTED"
        },
        "summary": summary,
        "approvedDemandLines": approved_lines,
        "heldDemandLines": quarantined_lines,
        "emergencyBrakeReasons": brake_reasons,
        "pendingApprovalRequests": approval_requests
    }
    return envelope


def main():
    parser = argparse.ArgumentParser(description="Evaluate governance brake and exceptions for x_OSQ")
    parser.add_argument("--input", "-i", required=True, help="Path to input QUIPU curated JSON")
    parser.add_argument("--mode", "-m", default="adaptive", choices=["strict", "adaptive", "permissive"], help="Governance profile")
    parser.add_argument("--output", "-o", default="out/04_governance_decision.json", help="Path to decision output JSON")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[ERROR] Input file {in_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = evaluate_governance_brake(data, args.mode)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    status_str = "TRIPPED (Held Exceptions)" if result['summary']['emergencyBrakeTripped'] else "CLEAR (All Approved)"
    print(f"[PASS] OSQ Governance Gate: Emergency Brake {status_str}, {result['summary']['linesApproved']} lines approved, {result['summary']['linesHeldForApproval']} held -> {out_path}")


if __name__ == "__main__":
    main()
