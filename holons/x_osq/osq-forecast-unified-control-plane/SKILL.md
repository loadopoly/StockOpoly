---
name: osq-forecast-unified-control-plane
description: Unified control plane for the x_OSQ Order, Sales, Quote & Intelligent Forecast Governance Holon. Orchestrates end-to-end demand intake, Snowflake retirement cleansing, QUIPU participant intelligence, Emergency Brake governance, Blue Yonder weekly bucket transformation, and SAP reservation synchronization.
---
# OSQ Forecast Unified Control Plane

## Objective
Provide the authoritative orchestration narrative and contract enforcement for the `x_osq` Holon within Holon Hub.

Coordinates:
- Stage 1: [osq-intake-normalizer](../osq-intake-normalizer/SKILL.md) — Multi-source quote ingestion & accepted-quote filtering.
- Stage 2: [osq-forecast-cleanser](../osq-forecast-cleanser/SKILL.md) — Current-month proration, delta detection, duplicate prevention (retires Snowflake logic).
- Stage 3: [osq-quipu-intelligence](../osq-quipu-intelligence/SKILL.md) — QUIPU participant worker: confidence scoring, 70%+ over-forecast suppression, SKU substitution impact, and lead-time simulation.
- Stage 4: [osq-governance-gate](../osq-governance-gate/SKILL.md) — Emergency Brake exception evaluation and held-task workflows.
- Stage 5: [osq-by-transformation-worker](../osq-by-transformation-worker/SKILL.md) — 26-week Blue Yonder bucket transformation and front-loading calculation.
- Stage 6: [osq-by-sap-connector](../osq-by-sap-connector/SKILL.md) — Blue Yonder connector feeds and SAP reservation synchronization.

## Contract Guarantees
1. HubCore owns the control plane, routing, governance, and events; QUIPU is a participant Holon providing intelligence and simulation.
2. Blue Yonder remains the planning and replenishment engine; QUIPU does not replace vendor calendars, safety stock models, or PO generation.
3. Front-loading occurs only after governance, simulation, and forecast quality controls are validated.
4. Accepted quotes are the only valid source of project demand.
5. SKU replacements transition demand without duplicate double-counting.
