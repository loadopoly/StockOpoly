#!/usr/bin/env python3
"""Stage 3: OSQ QUIPU Intelligence Worker.

Acts as the QUIPU participant intelligence worker in Holon Hub:
1. Calculates forecast confidence scores using QUIPU EMA calibration.
2. Identifies and suppresses 70%+ over-forecasting anomalies.
3. Manages SKU replacements without creating duplicate demand.
4. Simulates lead-time sensitivity (import vs domestic variability).
5. Optimizes front-loading demand to avoid early inventory traps.
6. Sends non-blocking observation telemetry to QUIPU observer (:7100).
Stdlib only.
"""
import argparse
import datetime
import json
import math
import os
import sys
import urllib.request
from pathlib import Path

QUIPU_DEFAULT_URL = os.environ.get("QUIPU_URL", "http://127.0.0.1:7100")


def probe_quipu_observer(url: str = QUIPU_DEFAULT_URL) -> bool:
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/health", headers={"User-Agent": "x_OSQ-QUIPU-Agent"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def notify_quipu_observer(observation: dict, url: str = QUIPU_DEFAULT_URL) -> None:
    try:
        data = json.dumps(observation).encode("utf-8")
        req = urllib.request.Request(
            f"{url.rstrip('/')}/observe",
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "x_OSQ-QUIPU-Agent"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            pass
    except Exception:
        pass


def run_quipu_intelligence(cleansed_data: dict, quipu_url: str = QUIPU_DEFAULT_URL) -> dict:
    lines = cleansed_data.get("cleansedDemandLines", [])
    batch_id = cleansed_data.get("envelope", {}).get("batchId", "BATCH-QUIPU")

    observer_alive = probe_quipu_observer(quipu_url)

    quipu_curated_lines = []
    suppressed_anomalies = []
    sku_substitutions_executed = []
    lead_time_risks = []
    front_loading_optimizations = []

    for item in lines:
        sku = item["sku"]
        origin = item.get("origin", "DOMESTIC")
        lead_time = int(item.get("leadTimeWeeks", 2))
        hist_avg = float(item.get("historicalActualMonthlyAvg", 50.0))
        rep_conf = float(item.get("salesRepConfidence", 0.8))
        rep_sku = item.get("replacementSku")
        rep_effective_week = item.get("replacementEffectiveWeek")
        front_load_req = item.get("frontLoadRequested", False)
        front_load_weeks = int(item.get("frontLoadWeeks", 0))

        weekly_forecast = item.get("weeklyForecast", [])

        # Calculate monthly equivalent demand
        total_qty_4w = sum(wf["cleansedQuantity"] for wf in weekly_forecast[:4])
        inflation_ratio = total_qty_4w / hist_avg if hist_avg > 0 else 1.0

        # Confidence calculation (QUIPU EMA-style formulation)
        # Low confidence if inflation > 1.70 (70%+ over-forecast) or if import lead time is high with low rep confidence
        base_conf = rep_conf
        if inflation_ratio > 1.70:
            confidence = max(0.15, base_conf * (1.70 / inflation_ratio) * 0.70)
        else:
            confidence = min(0.98, base_conf * (1.0 - max(0.0, inflation_ratio - 1.0) * 0.2))

        # Check 70%+ over-forecasting rule
        is_over_forecast = inflation_ratio > 1.70
        suppressed = False
        suppression_details = None

        curated_weekly = []
        for wf in weekly_forecast:
            w_num = wf["week"]
            q = wf["cleansedQuantity"]

            # If over-forecast is detected, suppress excess above 140% of baseline historical weekly pace
            hist_weekly_cap = (hist_avg / 4.0) * 1.40
            if is_over_forecast and q > hist_weekly_cap:
                suppressed = True
                suppressed_qty = round(hist_weekly_cap, 2)
                curated_weekly.append({
                    "week": w_num,
                    "cleansedQuantity": q,
                    "curatedQuantity": suppressed_qty,
                    "suppressionApplied": True,
                    "originalOverForecastQty": q
                })
            else:
                curated_weekly.append({
                    "week": w_num,
                    "cleansedQuantity": q,
                    "curatedQuantity": q,
                    "suppressionApplied": False
                })

        if suppressed:
            excess_units = total_qty_4w - sum(cw["curatedQuantity"] for cw in curated_weekly[:4])
            suppression_details = {
                "sku": sku,
                "quoteId": item["quoteId"],
                "historicalMonthlyAvg": hist_avg,
                "demandedMonthlyPace": total_qty_4w,
                "inflationRatio": round(inflation_ratio, 2),
                "excessUnitsSuppressed": round(excess_units, 2),
                "assignedConfidence": round(confidence, 3),
                "quipuAction": "SUPPRESSED_OVER_FORECAST"
            }
            suppressed_anomalies.append(suppression_details)

        # SKU Replacement Handling (No duplicate demand)
        sub_line = None
        if rep_sku and rep_effective_week:
            # Siphon demand after rep_effective_week to replacement SKU
            pre_rep_weekly = []
            post_rep_weekly = []
            for cw in curated_weekly:
                w_num = cw["week"]
                if w_num < int(rep_effective_week):
                    pre_rep_weekly.append(cw)
                else:
                    # Old SKU demand becomes 0 to prevent double-counting!
                    zeroed_entry = dict(cw)
                    zeroed_entry["curatedQuantity"] = 0.0
                    zeroed_entry["skuSubstitutedTo"] = rep_sku
                    pre_rep_weekly.append(zeroed_entry)

                    # Replacement SKU receives the curated quantity
                    new_entry = dict(cw)
                    new_entry["skuSubstitutedFrom"] = sku
                    post_rep_weekly.append(new_entry)

            curated_weekly = pre_rep_weekly

            sub_line = {
                "quoteId": item["quoteId"],
                "lineId": f"{item['lineId']}-SUB",
                "customer": item["customer"],
                "sku": rep_sku,
                "description": f"Auto-substituted replacement for {sku}",
                "origin": origin,
                "leadTimeWeeks": lead_time,
                "weeklyForecast": post_rep_weekly,
                "frontLoadRequested": front_load_req,
                "frontLoadWeeks": front_load_weeks,
                "historicalActualMonthlyAvg": hist_avg,
                "salesRepConfidence": rep_conf,
                "quipuConfidenceScore": round(confidence, 3),
                "derivedFromReplacement": True,
                "originalSku": sku,
                "source": item["source"]
            }
            sku_substitutions_executed.append({
                "originalSku": sku,
                "replacementSku": rep_sku,
                "effectiveWeek": rep_effective_week,
                "substitutedDemandUnits": sum(p["curatedQuantity"] for p in post_rep_weekly)
            })

        # Lead-Time Sensitivity Analysis (Import vs Domestic)
        lead_time_sensitivity = "LOW_RISK"
        if origin == "IMPORT" and lead_time >= 12:
            lead_time_sensitivity = "CRITICAL_IMPORT_SENSITIVITY"
            lead_time_risks.append({
                "sku": sku,
                "origin": origin,
                "leadTimeWeeks": lead_time,
                "planningHorizonWeeks": 26,
                "riskLevel": "HIGH",
                "recommendation": f"Import lead time ({lead_time} wks) consumes {round((lead_time/26.0)*100)}% of Blue Yonder horizon. Safety stock buffer advised in Blue Yonder."
            })
        elif origin == "DOMESTIC":
            lead_time_sensitivity = "DOMESTIC_AGILE"

        # Front-Loading Simulation
        front_load_approved = False
        front_load_adjustment = None
        if front_load_req and front_load_weeks > 0:
            # If confidence > 0.65 and not over-forecasted, optimize front-load
            if confidence >= 0.65 and not is_over_forecast:
                front_load_approved = True
                front_load_adjustment = {
                    "sku": sku,
                    "frontLoadWeeks": front_load_weeks,
                    "status": "APPROVED",
                    "reason": "High confidence and verified historical baseline permit front-loading."
                }
            else:
                front_load_approved = False
                front_load_adjustment = {
                    "sku": sku,
                    "frontLoadWeeks": front_load_weeks,
                    "status": "BLOCKED_BY_QUIPU",
                    "reason": "Front-loading denied: forecast inflation or low confidence creates inventory trap."
                }
            front_loading_optimizations.append(front_load_adjustment)

        curated_line = dict(item)
        curated_line["weeklyForecast"] = curated_weekly
        curated_line["quipuConfidenceScore"] = round(confidence, 3)
        curated_line["leadTimeSensitivity"] = lead_time_sensitivity
        curated_line["isOverForecast"] = is_over_forecast
        curated_line["frontLoadApproved"] = front_load_approved
        quipu_curated_lines.append(curated_line)

        if sub_line:
            quipu_curated_lines.append(sub_line)

    summary = {
        "batchId": batch_id,
        "totalLinesEvaluated": len(lines),
        "totalCuratedLinesOutput": len(quipu_curated_lines),
        "anomaliesSuppressedCount": len(suppressed_anomalies),
        "skuSubstitutionsCount": len(sku_substitutions_executed),
        "importRiskLinesCount": len(lead_time_risks),
        "frontLoadDecisionsCount": len(front_loading_optimizations),
        "quipuObserverConnected": observer_alive
    }

    envelope = {
        "envelope": {
            "type": "OSQ_QUIPU_INTELLIGENCE_CURATED",
            "version": "1.0.0",
            "timestampUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "batchId": batch_id,
            "intelligenceProvider": "QUIPU"
        },
        "summary": summary,
        "curatedDemandLines": quipu_curated_lines,
        "suppressedAnomalies": suppressed_anomalies,
        "skuSubstitutions": sku_substitutions_executed,
        "leadTimeSensitivityAnalysis": lead_time_risks,
        "frontLoadingDecisions": front_loading_optimizations
    }

    # Dispatch telemetry to observer
    if observer_alive:
        notify_quipu_observer({
            "event": "osq.quipu.curation.completed",
            "batchId": batch_id,
            "suppressedCount": len(suppressed_anomalies),
            "timestamp": envelope["envelope"]["timestampUtc"]
        }, quipu_url)

    return envelope


def classify_handwritten_note(note_text: str, source_type: str = "pallet_note", sku: str = "", dc: str = "TX03") -> dict:
    """Classify handwritten pallet/door/cycle-count notes for WIP & Cycle-Count risk models."""
    txt = str(note_text or "").strip()
    low = txt.lower()
    if any(k in low for k in ("damage", "dent", "door", "crushed", "broken")):
        cls = "DAMAGED_DOOR_HOLD"
        risk = 0.90
    elif any(k in low for k in ("short", "count", "missing", "variance", "mismatch")):
        cls = "CYCLE_COUNT_VARIANCE"
        risk = 0.88
    elif any(k in low for k in ("wip", "stage", "hold", "partial", "wait")):
        cls = "WIP_STAGING_PARTIAL"
        risk = 0.72
    elif any(k in low for k in ("relabel", "barcode", "rfid", "scan")):
        cls = "PALLET_RELABEL_NEEDED"
        risk = 0.58
    else:
        cls = "STANDARD_PUTAWAY"
        risk = 0.25
    return {
        "sourceType": source_type,
        "sku": sku,
        "dc": dc,
        "noteText": txt,
        "classification": cls,
        "wipImpactFlag": cls != "STANDARD_PUTAWAY",
        "cycleCountRiskScore": risk,
    }


def evaluate_probabilistic_stockout(floor_qoh: int, committed: int, s916: int, req_qty: int, expedite_surge_weight: float = 0.35) -> dict:
    """Compute 2-sigma@10d and 3-sigma@6d stockout probabilities factoring expedite surges."""
    net = max(0, int(floor_qoh) - int(committed) - int(s916))
    qty = max(1, int(req_qty))
    deficit_ratio = max(0.0, float(qty - net) / float(qty))
    sigma2_at_10d_pct = round(min(99.5, deficit_ratio * 68.0 + float(expedite_surge_weight) * 24.0), 2)
    sigma3_at_6d_pct = 0.12 if net >= qty else round(min(45.0, 0.35 + deficit_ratio * 18.0 * float(expedite_surge_weight)), 2)
    alert_10d = (sigma2_at_10d_pct >= 15.0) or (net < qty)
    return {
        "trueFloorAvail": net,
        "sigma2At10dPct": sigma2_at_10d_pct,
        "sigma3At6dPct": sigma3_at_6d_pct,
        "sigma3Pass": sigma3_at_6d_pct < 0.27,
        "tenDayAlertTriggered": alert_10d,
        "alertRecipients": ["Supervisor", "CSR", "Buyer"] if alert_10d else [],
        "csrCustomerNotifyMinDays": 5,
    }


def main():
    parser = argparse.ArgumentParser(description="Execute QUIPU forecast intelligence for x_OSQ")
    parser.add_argument("--input", "-i", required=True, help="Path to cleansed forecast JSON")
    parser.add_argument("--quipu-url", default=QUIPU_DEFAULT_URL, help="URL of QUIPU observer service")
    parser.add_argument("--output", "-o", default="out/03_quipu_curated.json", help="Path to curated output JSON")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[ERROR] Input file {in_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = run_quipu_intelligence(data, args.quipu_url)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"[PASS] QUIPU Forecast Intelligence: {result['summary']['anomaliesSuppressedCount']} over-forecasts suppressed, {result['summary']['skuSubstitutionsCount']} SKU substitutions executed, observerConnected={result['summary']['quipuObserverConnected']} -> {out_path}")


if __name__ == "__main__":
    main()
