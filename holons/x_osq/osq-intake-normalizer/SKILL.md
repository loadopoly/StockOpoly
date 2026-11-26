---
name: osq-intake-normalizer
description: Ingests and normalizes CPQ, Salesforce, and SAP quotes and orders. Filters demand to accepted-quote status, enforces the 6-month (26-week) planning horizon, and outputs canonical demand envelopes.
---
# OSQ Intake Normalizer

## Objective
Read diverse upstream forecast and quote sources (CPQ, Salesforce, SAP, CSV/JSON extracts) and normalize them into a uniform schema.

## Rules
1. **Accepted-Quote Filtering**: Only quotes marked `ACCEPTED` (or flagged `FORECAST_ELIGIBLE`) enter the demand stream. Draft, pending, or rejected bids are quarantined.
2. **6-Month Horizon**: Blue Yonder operates on a rolling 26-week horizon. Demands extending beyond week 26 are sliced out to prevent stale commitments.
3. **Traceability**: Retain origin system identifier (`quoteId`, `lineId`, `customer`) for downstream auditability.

Script:
`scripts/normalize_intake.py`
