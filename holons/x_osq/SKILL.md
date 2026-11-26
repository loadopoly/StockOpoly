---
name: x-osq
description: x_OSQ Holon - HolonHubLive Order, Sales, Quote & Intelligent Forecast Governance Holon for Holon Hub. Ingests CPQ / Salesforce / SAP quote feeds, pulls direct API time-phased QOH (restricting Snowflake to analytical storage), routes across physical-only DCs via Weighted Greedy Dijkstra without virtual warehouses, enforces 2-sigma/10d & 3-sigma/6d stockout probability guardrails, runs IFR auto-PO & cancellation-to-receiving-quarantine reconciliation, tracks Rust RFID IS/CS transit telemetry, engages QUIPU note classification & 70%+ suppression, bridges Stockopoly slotting & SAP signals, and synchronizes Blue Yonder & SAP reservations.
---
# x_OSQ — Order, Sales, Quote & Intelligent Forecast Governance Holon (`HolonHubLive`)

## 1. Architectural Role in Holon Hub
`x_osq` (v2.5.0, manifest [holon.json](holon.json)) is a sovereign Node A domain Holon (`node-a-osq-home`) powered by **HolonHubLive (:8900)** (`holon/Start-HolonHubLive.ps1`) and federated MESH inside Holon Hub:

> **Core Architectural Principle:** The consolidated ecosystem operates as a **Dual-Speed Cyber-Physical Nervous System** over a **4-Tier Single-Writer Data Plane**: (1) a **`< 50 ms` Spinal Reflex Fast-Path** (`HolonHubLive :8900` + `SpatialBridge` `ENU_METER_WORLD_V1` + Tier 0 `osq_ephemeral.sqlite` / SSE / `BroadcastChannel`) where RFID scans, Bakugo metrology, Vision OCR, and Stockopoly slotting commit atomically to Tier 1 WAL + `_outbox` with zero LLM/lakehouse blocking; and (2) a **Governed Cortical Loop** streaming outbox cursors through `HubCore :8000` (`DurableMessageBus` + `IntakeForwarder` -> `QUIPU :7100`) to bifurcate realizations into **Systemic** ERP levers (`R2,R3,R4,R5,R7,R10`) vs. **Operational** SHA-1 deduplicated `body_directives` (`IMMEDIATE_CARD` for `priority >= 0.85`, `SHIFT_DIGEST` for `>= 0.65`) while `return-quarantine-governance` owns the 7th (`Return`) chain. Physical `RFID RECEIPT_SCAN` and `SAP PICK_COMPLETE_KNOCKOFF` automatically close matching directives (`body_feedback`), while Tier 2 Parquet/DuckDB (`Snowflake` analytical-export-only) and Tier 3 MESH run off the hot path. Virtual warehouses remain strictly prohibited.

```text
User Clients (x_Hub / x_Floor) ──[HTTPS POST + idempotencyKey + expectedRevision]──▶ HolonHubLive HTTP+SSE (:8900)
  └─ BroadcastChannel('x_holonhub_live_v2') (UI hints) & Offline Queue                    │
                                                                                           ▼
                                                                              HubCore (:8000 Air-Lock)
                                  ┌───────────────────────────┬──────────┬─────────────────┴─────────────┬──────────────────────┐
                                  ▼                           ▼          ▼                               ▼                      ▼
                     x_OSQ Authoritative Service        QUIPU (:7100) /  GARD Unified Inventory      IFR Auto-PO &          Stockopoly (:8181)
                     (Expected Version + Lease Fence)   WIP/Cycle Holons  Marketplace (:8600) &      Rust RFID Telemetry    & SAP Signal Bridge
                     ├── Authoritative SQLite + Outbox  (Note Classifier, Weighted Dijkstra Router   (Cancel->Quarantine,   (403 on raw adjust;
                     └── Separate Ephemeral SQLite       2σ/3σ Bands)     & Tokenized Sub-Orders)     IS/CS Nodal Map)       PR/PO/Pick knockoff)
```

---

## 2. Constraints & TX03/TX77 Multi-Hub Solutions Handled by x_OSQ + Holon Hub

| Operational Constraint | Legacy Flow (Snowflake / Manual) | HolonHubLive + Holon Hub Flow |
|---|---|---|
| **Static QOH & Snowflake Bus** | Static snapshot exported via Snowflake drops late receipts/reservations and stalls buyer reorder triggers. | **Direct API Time-Phased QOH:** Blue Yonder & HolonHubLive pull SAP date-phased `availableAtDate` directly; Snowflake serves analytical archiving only. |
| **Virtual Warehouse Risk (TX03/TX77)** | Staging virtual TX03 bins in TX77 gets hijacked by MRO backorders, inflating on-hand vs AP accuracy. | **Unified Physical Inventory Marketplace:** Zero virtual warehouses; Weighted Greedy Dijkstra ("peeling the onion" over traffic/population density edges) routes from physical DCs (`GET /api/v1/osq/marketplace/inventory`) with consolidation vs split-dropship cost differential. |
| **Single-Location Quote Constraint** | CPQ/SAP forces one quote -> one location, causing manual STOs and delayed milk runs. | **Tokenized Split Quotes (`quoteTokenHash`) & Modular BOM:** One parent quote hash binds split physical sub-orders (`quote.split_allocate`) and modular `MOD-1..N` bundles (`bom.flatten_modular`). |
| **Expedite Surges & Reactive Shortages** | Buyers cut POs weekly (Tuesdays); late stockouts blindside CSRs & customers at pick wave. | **Probabilistic Stockout Bands (`2σ@10d` / `3σ@6d`) & IFR Auto-PO:** Flags `>0.27%` 5d risk at Day -10 for CSR+Buyer alignment, auto-cuts routine POs (`POST /api/v1/osq/ifr/auto-po`), and routes canceled orders to PO-reduction or Receiving Quarantine (`order.cancel_reconcile`). |
| **Duplicate STOs & Floor Tracking** | Identical STOs picked on the same day, unconsumed TX77 picks, handwritten labels, missing transit times. | **Same-Day STO `409` Guard & Rust RFID Telemetry:** Blocks duplicate `(order, sto, rdd)` without Supervisor override; tracks `IS`/`CS` RFID tags and updates Dijkstra edge weights on receipt scan; QUIPU classifies handwritten notes; Stockopoly maps exact physical bins; SAP Signal Bridge knocks off picks while blocking raw inventory edits (`403`). |
| **DC Kitting & FlexLogic (`flex_logic_kitting`)** | 9-step manual DC kitting (`TX03`, `TX77`, `NJ22`, `GA02`); paper `CO09` past-due PO traps & daily `ZSTOCK` edits. | Two-stage kitting check (`Var1=10d`/`Var2=6d`), `True Floor ATP`, Slide Step 8 Golden Rule (`WAIT_FOR_SUPPLY_PRESERVE_KIT`), 9-step workflow, printable Floor Kanban, and DeepMap event routing. |

---

## 3. Stages and Skills Inventory

| Stage # | Stage Skill | Function | Mode |
|---|---|---|---|
| 0 | [osq-forecast-unified-control-plane](osq-forecast-unified-control-plane/SKILL.md) | Control plane narrative and contract verification | orchestration |
| 1 | [osq-intake-normalizer](osq-intake-normalizer/SKILL.md) | Normalizes CPQ/Salesforce/SAP; filters accepted quotes; enforces 26-week horizon | read-only |
| 2 | [osq-forecast-cleanser](osq-forecast-cleanser/SKILL.md) | Direct API time-phased QOH, proration, deltas, dedup (Snowflake analytics-only) | transformation |
| 3 | [osq-quipu-intelligence](osq-quipu-intelligence/SKILL.md) | QUIPU participant: confidence, 70%+ suppression, note classification, CTO propensity, lead times | intelligence |
| 4 | [osq-governance-gate](osq-governance-gate/SKILL.md) | Emergency Brake, `2σ@10d` / `3σ@6d` stockout alerts, same-day duplicate STO guard | governance-gate |
| 5 | [osq-by-transformation-worker](osq-by-transformation-worker/SKILL.md) | 26-week Blue Yonder buckets, modular BOM flatten, Weighted Dijkstra split allocation | transformation |
| 6 | [osq-by-sap-connector](osq-by-sap-connector/SKILL.md) | Direct Blue Yonder API, SAP Signal Bridge, IFR auto-PO/quarantine, RFID telemetry, Stockopoly | connector-sync |

---

## 4. Routing Guide

| User Intent | Stages Activated | Primary Output |
|---|---|---|
| "Ingest quotes and check accepted status" | 1 | `01_intake_normalized.json` |
| "Cleanse forecast and check deltas vs last week" | 1 → 2 | `02_forecast_cleansed.json` |
| "Run QUIPU forecast intelligence and over-forecast check" | 1 → 2 → 3 | `03_quipu_curated.json` |
| "Check Emergency Brake and exception status" | 1 → 2 → 3 → 4 | `04_governance_decision.json` |
| "Transform demand for Blue Yonder weekly buckets" | 1 → 2 → 3 → 4 → 5 | `05_blue_yonder_transformed.json` |
| "Generate Blue Yonder feed and SAP reservations" | 1 → 2 → 3 → 4 → 5 → 6 | `06_connector_sync_manifest.json` |
| "Execute full end-to-end forecast pipeline" | 0 through 6 | Complete publication package |

---

## 5. Holon Runner CLI & HolonHubLive Reference

```bash
# Start HolonHubLive sovereign runtime (:8900)
powershell -NoProfile -ExecutionPolicy Bypass -File holon/Start-HolonHubLive.ps1 -Background -Port 8900 -SelfTest

# Run end-to-end headless forecast pipeline
python holon/x_osq.py run -i holon/samples/sample_cpq_quotes.json -p holon/samples/sample_previous_publication.json --mode adaptive
```
