---
name: osq-by-transformation-worker
description: Transforms approved demand into Blue Yonder's canonical 26-week (6-month) replenishment bucket structure. Aggregates multi-quote demand and applies authorized front-loading shifts.
---
# OSQ Blue Yonder Transformation Worker

## Objective
Structure cleansed and approved demand into the exact contract expected by Blue Yonder:
- **Weekly Buckets (W01-W26)**: Align demand across Blue Yonder's weekly replenishment cadence over a rolling 6-month horizon.
- **Front-Loading Realization**: Shift demand earlier for approved front-loading requests, providing inventory buffer while avoiding stockouts.
- **SKU Aggregation**: Consolidate disparate quote lines across customers into single unified weekly demand buckets per SKU and plant.

Script:
`scripts/transform_blueyonder.py`
