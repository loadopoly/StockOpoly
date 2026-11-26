#!/usr/bin/env python3
"""x_OSQ Holon Runner.

Chains the OSQ stages into an end-to-end pipeline and wraps every stage output in
HubCore Task / Result / Event / Artifact envelopes. Stdlib only.
No external network dependencies required (degrades gracefully if QUIPU observer is offline).

Key capabilities:
  * Ingests CPQ / Salesforce / SAP quotes and filters accepted demand within a 26-week horizon.
  * Cleanses forecast (proration, deltas, deduplication) to retire legacy Snowflake logic.
  * Engages QUIPU participant worker for 70%+ over-forecast suppression, confidence scoring,
    SKU replacements without duplicate demand, and lead-time simulation.
  * Evaluates Emergency Brake exception gate and approval workflows.
  * Transforms demand into Blue Yonder 26-week replenishment buckets with front-loading realization.
  * Synchronizes Blue Yonder connector export and SAP material reservations.
"""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "holon.json").read_text(encoding="utf-8"))
HOLON_ID = MANIFEST["holon"]["id"]

SCRIPTS = {
    "intake": "osq-intake-normalizer/scripts/normalize_intake.py",
    "cleanse": "osq-forecast-cleanser/scripts/cleanse_forecast.py",
    "quipu": "osq-quipu-intelligence/scripts/quipu_forecast_agent.py",
    "gate": "osq-governance-gate/scripts/governance_brake.py",
    "transform": "osq-by-transformation-worker/scripts/transform_blueyonder.py",
    "sync": "osq-by-sap-connector/scripts/sync_connectors.py"
}


def build_hubcore_envelope(
    kind: str,
    producer: str,
    payload: dict,
    operation: str = "execute",
    correlation_id: str = None,
    causation_id: str = None,
) -> dict:
    tid = f"task-{uuid.uuid4().hex[:12]}"
    rid = f"res-{uuid.uuid4().hex[:12]}"
    eid = f"evt-{uuid.uuid4().hex[:12]}"
    aid = f"art-{kind}-{uuid.uuid4().hex[:8]}"
    corr_id = correlation_id or f"corr-{uuid.uuid4().hex[:12]}"
    caus_id = causation_id or tid
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    raw_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    content_hash = hashlib.sha256(raw_bytes).hexdigest()

    return {
        "correlation_id": corr_id,
        "causation_id": caus_id,
        "task_id": tid,
        "task": {
            "taskId": tid,
            "correlation_id": corr_id,
            "causation_id": caus_id,
            "source": "x_hubcore",
            "target": producer,
            "operation": operation,
            "state": "COMPLETED",
            "timestampUtc": now
        },
        "result": {
            "resultId": rid,
            "taskId": tid,
            "correlation_id": corr_id,
            "causation_id": caus_id,
            "producer": producer,
            "status": "SUCCESS",
            "contentHash": content_hash,
            "payload": payload
        },
        "event": {
            "eventId": eid,
            "taskId": tid,
            "correlation_id": corr_id,
            "causation_id": caus_id,
            "type": f"osq.{kind}.completed",
            "producer": producer,
            "timestampUtc": now
        },
        "artifact": {
            "artifactId": aid,
            "taskId": tid,
            "correlation_id": corr_id,
            "causation_id": caus_id,
            "type": f"application/json;kind={kind}",
            "sha256": content_hash
        }
    }


def run_stage_script(script_key: str, in_file: str, out_file: str, extra_args: list = None) -> dict:
    script_rel = SCRIPTS.get(script_key)
    if not script_rel:
        raise ValueError(f"Unknown stage script key: {script_key}")

    script_path = ROOT / script_rel
    if not script_path.exists():
        raise FileNotFoundError(f"Missing script: {script_path}")

    cmd = [sys.executable, str(script_path), "-i", in_file, "-o", out_file]
    if extra_args:
        cmd.extend(extra_args)

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print(f"[ERROR in {script_key}]: {res.stderr}", file=sys.stderr)
        raise RuntimeError(f"Stage {script_key} failed with return code {res.returncode}")

    if res.stdout.strip():
        print(f"[{script_key.upper()}]: {res.stdout.strip()}")

    with open(out_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def run_pipeline(input_quotes_path: str, previous_pub_path: str = None, mode: str = "adaptive", plant: str = "PLANT-1010", out_dir: str = "out") -> dict:
    out_base = ROOT / out_dir
    out_base.mkdir(parents=True, exist_ok=True)

    print(f"=== Starting x_OSQ Forecast & Integration Pipeline [Mode: {mode.upper()}] ===")

    # 1. Intake Normalization
    f_intake = str(out_base / "01_intake_normalized.json")
    intake_data = run_stage_script("intake", input_quotes_path, f_intake)

    # 2. Cleansing (Snowflake Retirement)
    f_cleanse = str(out_base / "02_forecast_cleansed.json")
    cleanse_args = ["-p", previous_pub_path] if previous_pub_path else []
    cleanse_data = run_stage_script("cleanse", f_intake, f_cleanse, cleanse_args)

    # 3. QUIPU Intelligence & Curation
    f_quipu = str(out_base / "03_quipu_curated.json")
    quipu_data = run_stage_script("quipu", f_cleanse, f_quipu)

    # 4. Governance Gate & Emergency Brake
    f_gate = str(out_base / "04_governance_decision.json")
    gate_data = run_stage_script("gate", f_quipu, f_gate, ["-m", mode])

    # 5. Blue Yonder Transformation
    f_by = str(out_base / "05_blue_yonder_transformed.json")
    by_data = run_stage_script("transform", f_gate, f_by)

    # 6. Integration Connectors (BY & SAP)
    f_sync = str(out_base / "06_connector_sync_manifest.json")
    sync_data = run_stage_script("sync", f_by, f_sync, ["--plant", plant])

    # Build master HubCore envelope
    summary_report = {
        "holonId": HOLON_ID,
        "version": MANIFEST["holon"]["version"],
        "pipelineMode": mode,
        "inputSourceQuotes": intake_data["summary"]["total_quotes_received"],
        "acceptedQuotes": intake_data["summary"]["accepted_quotes"],
        "ineligibleQuotesFiltered": intake_data["summary"]["ineligible_quotes"],
        "duplicatesEliminated": cleanse_data["summary"]["duplicatesEliminated"],
        "deltasTracked": cleanse_data["summary"]["deltasIdentified"],
        "quipuOverForecastSuppressed": quipu_data["summary"]["anomaliesSuppressedCount"],
        "skuSubstitutions": quipu_data["summary"]["skuSubstitutionsCount"],
        "importRiskLines": quipu_data["summary"]["importRiskLinesCount"],
        "emergencyBrakeTripped": gate_data["summary"]["emergencyBrakeTripped"],
        "linesApproved": gate_data["summary"]["linesApproved"],
        "linesHeld": gate_data["summary"]["linesHeldForApproval"],
        "blueYonderSkusPublished": by_data["summary"]["uniqueSkusPublished"],
        "blueYonderTotalUnits": by_data["summary"]["totalUnitsPublished"],
        "sapReservationsGenerated": sync_data["summary"]["sapReservationsCount"],
        "artifactsGenerated": [
            f_intake,
            f_cleanse,
            f_quipu,
            f_gate,
            f_by,
            f_sync
        ]
    }

    master_envelope = build_hubcore_envelope("forecast_pipeline", HOLON_ID, summary_report, "run-all")
    f_master = out_base / "00_osq_master_envelope.json"
    with open(f_master, "w", encoding="utf-8") as f:
        json.dump(master_envelope, f, indent=2)

    print(f"=== Pipeline Completed Successfully. Master Envelope: {f_master} ===")
    return master_envelope


def main():
    parser = argparse.ArgumentParser(description="x_OSQ Holon Runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run all
    p_run = subparsers.add_parser("run", help="Run end-to-end OSQ pipeline")
    p_run.add_argument("--input", "-i", required=True, help="Path to input raw quotes JSON")
    p_run.add_argument("--previous", "-p", default=None, help="Path to previous publication JSON")
    p_run.add_argument("--mode", "-m", default="adaptive", choices=["strict", "adaptive", "permissive"], help="Governance profile")
    p_run.add_argument("--plant", default="PLANT-1010", help="Plant identifier")
    p_run.add_argument("--outdir", "-o", default="out", help="Output directory")

    # individual stages
    p_intake = subparsers.add_parser("intake", help="Stage 1: Normalize intake")
    p_intake.add_argument("--input", "-i", required=True)
    p_intake.add_argument("--output", "-o", default="out/01_intake_normalized.json")

    p_cleanse = subparsers.add_parser("cleanse", help="Stage 2: Cleanse forecast")
    p_cleanse.add_argument("--input", "-i", required=True)
    p_cleanse.add_argument("--previous", "-p", default=None)
    p_cleanse.add_argument("--output", "-o", default="out/02_forecast_cleansed.json")

    p_quipu = subparsers.add_parser("quipu", help="Stage 3: QUIPU intelligence curation")
    p_quipu.add_argument("--input", "-i", required=True)
    p_quipu.add_argument("--output", "-o", default="out/03_quipu_curated.json")

    p_gate = subparsers.add_parser("gate", help="Stage 4: Governance Emergency Brake")
    p_gate.add_argument("--input", "-i", required=True)
    p_gate.add_argument("--mode", "-m", default="adaptive")
    p_gate.add_argument("--output", "-o", default="out/04_governance_decision.json")

    p_transform = subparsers.add_parser("transform", help="Stage 5: Blue Yonder transformation")
    p_transform.add_argument("--input", "-i", required=True)
    p_transform.add_argument("--output", "-o", default="out/05_blue_yonder_transformed.json")

    p_sync = subparsers.add_parser("sync", help="Stage 6: Connectors synchronization")
    p_sync.add_argument("--input", "-i", required=True)
    p_sync.add_argument("--plant", default="PLANT-1010")
    p_sync.add_argument("--output", "-o", default="out/06_connector_sync_manifest.json")

    args = parser.parse_args()

    if args.command == "run":
        run_pipeline(args.input, args.previous, args.mode, args.plant, args.outdir)
    elif args.command == "intake":
        run_stage_script("intake", args.input, args.output)
    elif args.command == "cleanse":
        extra = ["-p", args.previous] if args.previous else []
        run_stage_script("cleanse", args.input, args.output, extra)
    elif args.command == "quipu":
        run_stage_script("quipu", args.input, args.output)
    elif args.command == "gate":
        run_stage_script("gate", args.input, args.output, ["-m", args.mode])
    elif args.command == "transform":
        run_stage_script("transform", args.input, args.output)
    elif args.command == "sync":
        run_stage_script("sync", args.input, args.output, ["--plant", args.plant])


if __name__ == "__main__":
    main()
