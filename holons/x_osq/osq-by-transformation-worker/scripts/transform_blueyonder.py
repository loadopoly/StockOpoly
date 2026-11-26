#!/usr/bin/env python3
"""Stage 5: OSQ Blue Yonder Transformation Worker.

Transforms governed, cleansed, and QUIPU-curated demand into Blue Yonder's
canonical weekly replenishment bucket format:
1. Slices into 26 weekly planning buckets (6-month planning horizon).
2. Applies optimized front-loading shifts for approved lines.
3. Consolidates demand across multiple quotes into unified SKU buckets.
4. Prepares Blue Yonder staging artifact. Stdlib only.
"""
import argparse
import datetime
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

MAX_HORIZON_WEEKS = 26


def transform_to_blue_yonder(gov_data: dict) -> dict:
    lines = gov_data.get("approvedDemandLines", [])
    batch_id = gov_data.get("envelope", {}).get("batchId", "BATCH-BY")

    # SKU -> week_num -> total_quantity
    sku_buckets = defaultdict(lambda: [0.0] * (MAX_HORIZON_WEEKS + 1))
    sku_meta = {}
    front_load_log = []

    for item in lines:
        sku = item["sku"]
        origin = item.get("origin", "DOMESTIC")
        lead_time = int(item.get("leadTimeWeeks", 2))
        front_load_approved = item.get("frontLoadApproved", False)
        front_load_weeks = int(item.get("frontLoadWeeks", 0))

        if sku not in sku_meta:
            sku_meta[sku] = {
                "sku": sku,
                "description": item.get("description", ""),
                "origin": origin,
                "leadTimeWeeks": lead_time,
                "confidenceScore": item.get("quipuConfidenceScore", 0.8)
            }

        weekly = item.get("weeklyForecast", [])

        # Apply front-loading if approved
        if front_load_approved and front_load_weeks > 0:
            for wf in weekly:
                orig_w = wf["week"]
                q = wf["curatedQuantity"]
                target_w = max(1, orig_w - front_load_weeks)
                if target_w <= MAX_HORIZON_WEEKS:
                    sku_buckets[sku][target_w] += q
                front_load_log.append({
                    "sku": sku,
                    "quoteId": item["quoteId"],
                    "originalWeek": orig_w,
                    "targetWeek": target_w,
                    "shiftedQuantity": q,
                    "leadTimeWeeks": lead_time
                })
        else:
            for wf in weekly:
                w_num = wf["week"]
                q = wf["curatedQuantity"]
                if 1 <= w_num <= MAX_HORIZON_WEEKS:
                    sku_buckets[sku][w_num] += q

    # Build canonical Blue Yonder publication payload
    by_publication = []
    total_published_units = 0.0

    for sku, buckets in sorted(sku_buckets.items()):
        meta = sku_meta[sku]
        weekly_allocations = {}
        sku_total = 0.0
        for w in range(1, MAX_HORIZON_WEEKS + 1):
            qty = round(buckets[w], 2)
            weekly_allocations[f"W{w:02d}"] = qty
            sku_total += qty
            total_published_units += qty

        by_publication.append({
            "sku": sku,
            "description": meta["description"],
            "origin": meta["origin"],
            "leadTimeWeeks": meta["leadTimeWeeks"],
            "confidenceScore": meta["confidenceScore"],
            "total6MonthHorizonUnits": round(sku_total, 2),
            "weeklyBuckets": weekly_allocations
        })

    summary = {
        "batchId": batch_id,
        "planningHorizonWeeks": MAX_HORIZON_WEEKS,
        "planningHorizonMonths": 6,
        "uniqueSkusPublished": len(by_publication),
        "totalUnitsPublished": round(total_published_units, 2),
        "frontLoadedLinesCount": len(front_load_log),
        "targetEngine": "Blue Yonder Demand / Replenishment"
    }

    envelope = {
        "envelope": {
            "type": "OSQ_BLUE_YONDER_TRANSFORMED",
            "version": "1.0.0",
            "timestampUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "batchId": batch_id,
            "targetSystem": "BLUE_YONDER"
        },
        "summary": summary,
        "blueYonderDemandPublication": by_publication,
        "frontLoadingAudit": front_load_log
    }
    return envelope


def main():
    parser = argparse.ArgumentParser(description="Transform governed forecast to Blue Yonder weekly buckets")
    parser.add_argument("--input", "-i", required=True, help="Path to input governance decision JSON")
    parser.add_argument("--output", "-o", default="out/05_blue_yonder_transformed.json", help="Path to BY transformed output JSON")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[ERROR] Input file {in_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = transform_to_blue_yonder(data)
    result.setdefault("tx03Tx77Architecture", {
        "snowflakeRole": "analytical-export-only",
        "directSapApiTimePhased": True,
        "virtualWarehousesAllowed": False,
        "snowflakeOperationalFeedbackBypassed": True,
    })

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"[PASS] Blue Yonder Transformation: {result['summary']['uniqueSkusPublished']} SKUs transformed across 26 weeks, {result['summary']['totalUnitsPublished']} total units -> {out_path}")


if __name__ == "__main__":
    main()
