#!/usr/bin/env python3
"""Stage 1: OSQ Forecast Intake Normalizer.

Ingests CPQ, Salesforce, and SAP quote feeds, applies accepted-quote eligibility
gating, checks the 6-month planning horizon (W1-W26), and outputs a normalized
canonical demand stream envelope. Stdlib only.
"""
import argparse
import datetime
import json
import os
import sys
import uuid
from pathlib import Path

MAX_HORIZON_WEEKS = 26


def normalize_intake(raw_data: dict) -> dict:
    source_sys = raw_data.get("sourceSystem", "UNKNOWN_SOURCE")
    batch_id = raw_data.get("batchId", f"BATCH-{uuid.uuid4().hex[:8]}")
    quotes = raw_data.get("quotes", [])

    accepted_lines = []
    rejected_lines = []
    summary_counts = {
        "total_quotes_received": len(quotes),
        "accepted_quotes": 0,
        "ineligible_quotes": 0,
        "total_lines_accepted": 0,
        "total_lines_ineligible": 0,
        "total_units_raw": 0,
    }

    for q in quotes:
        quote_id = q.get("quoteId", "UNKNOWN_QUOTE")
        status = str(q.get("status", "")).upper()
        is_accepted = (status == "ACCEPTED" or q.get("eligibility") == "FORECAST_ELIGIBLE")

        if not is_accepted:
            summary_counts["ineligible_quotes"] += 1
            for line in q.get("lines", []):
                summary_counts["total_lines_ineligible"] += 1
                rejected_lines.append({
                    "quoteId": quote_id,
                    "lineId": line.get("lineId"),
                    "sku": line.get("sku"),
                    "reason": f"Quote status '{status}' is ineligible for forecast publication (accepted quotes only)."
                })
            continue

        summary_counts["accepted_quotes"] += 1

        for line in q.get("lines", []):
            line_id = line.get("lineId", str(uuid.uuid4())[:8])
            sku = line.get("sku", "UNKNOWN_SKU")
            origin = str(line.get("origin", "DOMESTIC")).upper()
            lead_time = int(line.get("leadTimeWeeks", 2))
            hist_avg = float(line.get("historicalActualMonthlyAvg", 50.0))
            rep_conf = float(line.get("salesRepConfidence", 0.8))
            front_load_req = bool(line.get("frontLoadRequested", False))
            front_load_weeks = int(line.get("frontLoadWeeks", 0))
            rep_sku = line.get("replacementSku")
            rep_week = line.get("replacementEffectiveWeek")

            # Validate weekly forecast within 26-week horizon
            raw_weeks = line.get("weeklyForecast", [])
            valid_weekly = []
            for wf in raw_weeks:
                w_num = int(wf.get("week", 1))
                qty = float(wf.get("quantity", 0))
                summary_counts["total_units_raw"] += qty
                if 1 <= w_num <= MAX_HORIZON_WEEKS:
                    valid_weekly.append({
                        "week": w_num,
                        "quantity": qty,
                        "currentMonthProrated": bool(wf.get("currentMonthProrated", False))
                    })

            record = {
                "quoteId": quote_id,
                "lineId": line_id,
                "customer": q.get("customer", "UNKNOWN_CUSTOMER"),
                "sku": sku,
                "description": line.get("description", ""),
                "origin": origin,
                "leadTimeWeeks": lead_time,
                "weeklyForecast": valid_weekly,
                "frontLoadRequested": front_load_req,
                "frontLoadWeeks": front_load_weeks,
                "historicalActualMonthlyAvg": hist_avg,
                "salesRepConfidence": rep_conf,
                "replacementSku": rep_sku,
                "replacementEffectiveWeek": rep_week,
                "source": q.get("source", source_sys)
            }
            accepted_lines.append(record)
            summary_counts["total_lines_accepted"] += 1

    envelope = {
        "envelope": {
            "type": "OSQ_INTAKE_STREAM",
            "version": "1.0.0",
            "timestampUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "batchId": batch_id,
            "horizonWeeks": MAX_HORIZON_WEEKS
        },
        "summary": summary_counts,
        "acceptedDemandLines": accepted_lines,
        "ineligibleExclusions": rejected_lines
    }
    return envelope


def main():
    parser = argparse.ArgumentParser(description="Normalize CPQ/Salesforce/SAP intake feeds for x_OSQ")
    parser.add_argument("--input", "-i", required=True, help="Path to input raw quotes JSON")
    parser.add_argument("--output", "-o", default="out/01_intake_normalized.json", help="Path to normalized output JSON")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[ERROR] Input file {in_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    with open(in_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    result = normalize_intake(raw)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"[PASS] OSQ Intake Normalized: {result['summary']['total_lines_accepted']} lines accepted, {result['summary']['total_lines_ineligible']} ineligible filtered -> {out_path}")


if __name__ == "__main__":
    main()
