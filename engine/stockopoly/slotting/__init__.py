"""Slotting suite: velocity → occupancy → optimizer → migration → safety stock."""
from __future__ import annotations

from .migration import build_tasks, complete_task, list_tasks
from .occupancy import compute_occupancy, current_state
from .optimizer import list_plans, optimize, plan_assignments
from .safety_stock import (SCENARIO_Z, compute_safety_stock, list_ss,
                           scenario_compare)
from .velocity import compute_velocity, list_velocity

__all__ = [
    "compute_velocity", "list_velocity",
    "compute_occupancy", "current_state",
    "optimize", "plan_assignments", "list_plans",
    "build_tasks", "list_tasks", "complete_task",
    "compute_safety_stock", "scenario_compare", "list_ss", "SCENARIO_Z",
]
