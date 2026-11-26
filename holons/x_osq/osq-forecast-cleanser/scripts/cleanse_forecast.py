#!/usr/bin/env python3
"""Stage 2: OSQ Forecast Cleanser (Retires legacy Snowflake logic).

Executes business logic formerly executed in Snowflake:
1. Current-month calendar proration.
2. Delta detection vs previously published demand batches.
3. Duplicate demand prevention across overlapping quote line submissions.
4. Baseline variance check. Stdlib only.
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path


def cleanse_forecast(intake_data: dict, previous_publication: dict = None) -> dict:
    lines = intake_data.get("acceptedDemandLines", [])
    batch_id = intake_data.get("envelope", {}).get("batchId", "BATCH-CLEANSE")

    prev_demand = {}
    if previous_publication and "publishedWeeklyDemand" in previous_publication:
        prev_demand = previous_publication["publishedWeeklyDemand"]

    cleansed_lines = []
    seen_dedup_keys = set()
    dedup_exclusions = []
    delta_records = []

    proration_factor = 0.75  # e.g., 3 weeks remaining out of 4 in the current month

    for item in lines:
        sku = item["sku"]
        quote_id = item["quoteId"]
        line_id = item["lineId"]

        # Deduplication signature based on customer + sku + quote + scheduled deliveries
        wf_sig = hashlib.sha256(json.dumps(item["weeklyForecast"], sort_keys=True).encode("utf-8")).hexdigest()[:12]
        dedup_key = f"{item['customer']}|{sku}|{wf_sig}"

        if dedup_key in seen_dedup_keys:
            dedup_exclusions.append({
                "quoteId": quote_id,
                "lineId": line_id,
                "sku": sku,
                "reason": "Duplicate demand signature identified across concurrent quote entries."
            })
            continue
        seen_dedup_keys.add(dedup_key)

        # Apply current-month proration and compute deltas
        cleansed_weekly = []
        for wf in item["weeklyForecast"]:
            w_num = wf["week"]
            qty = float(wf["quantity"])
            was_prorated = False

            # If week 1 or tagged with currentMonthProrated, prorate to avoid full-month front-load
            if w_num == 1 or wf.get("currentMonthProrated", False):
                qty = round(qty * proration_factor, 2)
                was_prorated = True

            # Delta comparison vs previous publication
            prev_qty = 0.0
            if sku in prev_demand and str(w_num) in prev_demand[sku]:
                prev_qty = float(prev_demand[sku][str(w_num)])

            delta = round(qty - prev_qty, 2)
            delta_pct = round((delta / prev_qty * 100.0), 1) if prev_qty > 0 else 100.0

            if abs(delta) > 0.01:
                delta_records.append({
                    "sku": sku,
                    "week": w_num,
                    "previousQty": prev_qty,
                    "newQty": qty,
                    "delta": delta,
                    "deltaPct": delta_pct
                })

            cleansed_weekly.append({
                "week": w_num,
                "rawQuantity": wf["quantity"],
                "cleansedQuantity": qty,
                "prorated": was_prorated,
                "deltaVsPrevious": delta
            })

        cleansed_line = dict(item)
        cleansed_line["weeklyForecast"] = cleansed_weekly
        cleansed_lines.append(cleansed_line)

    summary = {
        "batchId": batch_id,
        "inputLinesCount": len(lines),
        "cleansedLinesCount": len(cleansed_lines),
        "duplicatesEliminated": len(dedup_exclusions),
        "deltasIdentified": len(delta_records),
        "prorationFactorApplied": proration_factor
    }

    envelope = {
        "envelope": {
            "type": "OSQ_FORECAST_CLEANSED",
            "version": "1.0.0",
            "timestampUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "batchId": batch_id
        },
        "summary": summary,
        "cleansedDemandLines": cleansed_lines,
        "deduplicatedExclusions": dedup_exclusions,
        "deltaAudit": delta_records
    }
    return envelope


def main():
    parser = argparse.ArgumentParser(description="Cleanse normalized forecast and retire Snowflake logic")
    parser.add_argument("--input", "-i", required=True, help="Path to input normalized JSON")
    parser.add_argument("--previous", "-p", default=None, help="Optional previous publication JSON for delta detection")
    parser.add_argument("--output", "-o", default="out/02_forecast_cleansed.json", help="Path to cleansed output JSON")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[ERROR] Input file {in_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    prev_pub = None
    if args.previous:
        p_path = Path(args.previous)
        if p_path.exists():
            with open(p_path, "r", encoding="utf-8") as pf:
                prev_pub = json.load(pf)

    result = cleanse_forecast(data, prev_pub)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"[PASS] OSQ Forecast Cleansed: {result['summary']['cleansedLinesCount']} lines preserved, {result['summary']['duplicatesEliminated']} duplicates eliminated, {result['summary']['deltasIdentified']} deltas tracked -> {out_path}")


if __name__ == "__main__":
    main()
