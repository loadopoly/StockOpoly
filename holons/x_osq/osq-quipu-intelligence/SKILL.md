---
name: osq-quipu-intelligence
description: Engages QUIPU as an intelligent participant worker inside Holon Hub. Generates forecast confidence scores, suppresses 70%+ over-forecasting anomalies, simulates SKU replacement impact without duplicate demand, evaluates import vs domestic lead-time sensitivity, and optimizes front-loading requests.
---
# OSQ QUIPU Intelligence Worker

## Objective
Provide cognitive demand curation, simulation, and anomaly detection as a participant Holon inside Holon Hub without usurping Blue Yonder's replenishment role:
- **Confidence Scoring**: Compute confidence calibrated via the QUIPU observer EMA formula.
- **70%+ Over-Forecasting Suppression**: Detect sales-inflated forecast spikes (>1.7x historical baseline) and suppress the excess down to an adaptive threshold to prevent premature inventory build.
- **SKU Replacement Simulation**: Siphon demand from legacy parts to replacement parts at the effective cutoff week, zeroing out old SKU demand to prevent duplicate orders.
- **Lead-Time Sensitivity**: Analyze supply chain risk for long lead-time imports (e.g. 16+ weeks) vs fast domestic items (2-4 weeks).
- **Front-Loading Optimization**: Authorize front-loading only for items with high confidence and manageable inventory risk.
- **Telemetry**: Report observations asynchronously to `quipu-observer:7100`.

Script:
`scripts/quipu_forecast_agent.py`
