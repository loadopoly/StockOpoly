#!/usr/bin/env python3
"""Stage 6: OSQ Integration Connectors (Blue Yonder & SAP).

Orchestrates connector output payloads:
1. Generates Blue Yonder Weekly Demand Connector export.
2. Synchronizes SAP material reservation manifests for project demand.
3. Produces verifiable connector transaction manifests with SHA-256 hashes.
Stdlib only.
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path


def sync_connectors(by_data: dict, plant_id: str = "PLANT-1010") -> dict:
    batch_id = by_data.get("envelope", {}).get("batchId", "BATCH-CONNECTORS")
    by_pub = by_data.get("blueYonderDemandPublication", [])

    # 1. Blue Yonder Connector Feed
    by_records = []
    for item in by_pub:
        sku = item["sku"]
        lead_time = item["leadTimeWeeks"]
        conf = item["confidenceScore"]
        for week_bucket, qty in item["weeklyBuckets"].items():
            if qty > 0:
                by_records.append({
                    "recordType": "WEEKLY_DEMAND",
                    "plantId": plant_id,
                    "itemNumber": sku,
                    "planningBucket": week_bucket,
                    "quantity": qty,
                    "confidenceFactor": conf,
                    "leadTimeWeeks": lead_time,
                    "sourceHolon": "x_osq"
                })

    # 2. SAP Material Reservation Feed
    # Reserves immediate near-term demand (e.g. W01-W04) in SAP to lock inventory for accepted project commitments
    sap_reservations = []
    for item in by_pub:
        sku = item["sku"]
        w1_4_units = sum(item["weeklyBuckets"].get(f"W{w:02d}", 0.0) for w in range(1, 5))
        if w1_4_units > 0:
            res_id = f"RES-SAP-{hashlib.sha256(f'{batch_id}|{sku}'.encode()).hexdigest()[:8].upper()}"
            sap_reservations.append({
                "reservationNumber": res_id,
                "plant": plant_id,
                "storageLocation": "0001",
                "materialNumber": sku,
                "reservedQuantity": round(w1_4_units, 2),
                "requirementDate": (datetime.date.today() + datetime.timedelta(days=7)).isoformat(),
                "movementType": "261",  # Goods issue for order / project reservation
                "commitmentSource": "CPQ_ACCEPTED_QUOTE"
            })

    by_bytes = json.dumps(by_records, sort_keys=True).encode("utf-8")
    sap_bytes = json.dumps(sap_reservations, sort_keys=True).encode("utf-8")

    summary = {
        "batchId": batch_id,
        "plantId": plant_id,
        "blueYonderRecordsCount": len(by_records),
        "blueYonderPayloadSha256": hashlib.sha256(by_bytes).hexdigest(),
        "sapReservationsCount": len(sap_reservations),
        "sapPayloadSha256": hashlib.sha256(sap_bytes).hexdigest(),
        "status": "READY_FOR_PUBLICATION"
    }

    envelope = {
        "envelope": {
            "type": "OSQ_CONNECTOR_SYNC_MANIFEST",
            "version": "1.0.0",
            "timestampUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "batchId": batch_id,
            "connectors": ["BLUE_YONDER", "SAP_RESERVATIONS"]
        },
        "summary": summary,
        "blueYonderConnectorExport": by_records,
        "sapReservationSynchronization": sap_reservations
    }
    return envelope


def build_time_phased_sap_api_qoh(sku: str, dc: str, floor_qoh: int, committed: int, s916: int, in_transit_sto: int, shipper_eta_days: int, past_due_co09: int = 0) -> dict:
    """Direct SAP API time-phased QOH projection (bypassing Snowflake operational loop; Snowflake analytical-export-only)."""
    net0 = max(0, int(floor_qoh) - int(committed) - int(s916))
    return {
        "sku": sku,
        "dc": dc,
        "physicalOnly": True,
        "virtualWarehousesAllowed": False,
        "dataSource": "SAP_DIRECT_API",
        "snowflakeRole": "analytical-export-only",
        "excludedPastDueCo09Po": int(past_due_co09),
        "availableAtDate": {
            "T+0": net0,
            "T+3": net0 + (int(in_transit_sto) if int(shipper_eta_days) <= 3 else 0),
            "T+6": net0 + (int(in_transit_sto) if int(shipper_eta_days) <= 6 else 0),
            "T+10": net0 + (int(in_transit_sto) if int(shipper_eta_days) <= 10 else 0),
            "T+14": net0 + int(in_transit_sto),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Synchronize Blue Yonder and SAP integration connectors")
    parser.add_argument("--input", "-i", required=True, help="Path to input Blue Yonder transformed JSON")
    parser.add_argument("--plant", default="PLANT-1010", help="Target SAP / Blue Yonder plant identifier")
    parser.add_argument("--output", "-o", default="out/06_connector_sync_manifest.json", help="Path to connector manifest JSON")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[ERROR] Input file {in_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = sync_connectors(data, args.plant)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"[PASS] Connector Sync Manifest: {result['summary']['blueYonderRecordsCount']} BY records, {result['summary']['sapReservationsCount']} SAP reservations -> {out_path}")


if __name__ == "__main__":
    main()
