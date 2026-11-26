---
name: osq-forecast-cleanser
description: Cleanses normalized forecast streams and retires legacy Snowflake business logic. Executes current-month proration, delta detection against prior publications, duplicate prevention, and baseline quality scoring.
---
# OSQ Forecast Cleanser (Snowflake Logic Retirement)

## Objective
Migrate transformation rules out of Snowflake into a sovereign, testable Holon stage:
- **Current-Month Proration**: Automatically adjust week 1 / current month forecast volume based on remaining calendar days to prevent front-loaded distortions.
- **Delta Detection**: Compare line-by-line demand against the previously published batch, computing absolute and percentage shifts.
- **Duplicate Demand Elimination**: Detect identical quote submissions or overlapping customer demand signatures and eliminate duplicates before simulation.

Script:
`scripts/cleanse_forecast.py`
