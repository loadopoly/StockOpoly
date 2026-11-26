---
name: osq-by-sap-connector
description: Produces Blue Yonder connector publication exports and generates SAP material reservation synchronization manifests for project commitments.
---
# OSQ Integration Connectors (Blue Yonder & SAP)

## Objective
Finalize outbound integration artifacts for planning and ERP execution:
- **Blue Yonder Planning Export**: Prepare flat file / API payload containing weekly SKU bucket quantities, confidence metrics, and lead-time parameters for Blue Yonder replenishment runs.
- **SAP Material Reservations**: Generate movement type 261 reservation objects for near-term weeks (W01-W04) to secure physical inventory in SAP for committed projects, eliminating reconciliation mismatches between forecast and execution.
- **Verification Manifest**: Compute SHA-256 signatures for publication artifacts to ensure data integrity across the air-lock.

Script:
`scripts/sync_connectors.py`
