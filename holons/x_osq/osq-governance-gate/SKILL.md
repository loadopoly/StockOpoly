---
name: osq-governance-gate
description: Enforces the Emergency Brake and exception management policy for forecast publication. Evaluates confidence boundaries and volume limits, holding unverified demand and routing approval requests to supply chain planners.
---
# OSQ Forecast Governance Gate & Emergency Brake

## Objective
Act as the safety brake before publication to downstream planning systems:
- **Emergency Brake**: Automatically trip when confidence falls below the policy cutoff (e.g. 0.25 in adaptive mode, 0.40 in strict mode) or when import volumes exceed unapproved thresholds.
- **Held-Result Governance**: Transition tasks to `HELD` state in HubCore when the Emergency Brake is tripped, halting publication until explicit human-in-the-loop approval is granted.
- **Approval Workflows**: Generate actionable approval manifests detailing the risk, recommended action, and SKU parameters.
- **Audit Logging**: Maintain immutable audit logs of all governance decisions.

Script:
`scripts/governance_brake.py`
