# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Computed usage and count views for trajectory runs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from orchestrator._trajectory_view_models import TurnUsageView

if TYPE_CHECKING:
    from orchestrator._trajectory_run_model import TrajectoryRun


def tool_calls(run: TrajectoryRun) -> int:
    return sum(1 for step in run.steps if step.is_call)


def step_count(run: TrajectoryRun) -> int:
    return len(run.steps)


def model(run: TrajectoryRun) -> str:
    if run.run_usage is None or not run.run_usage.models:
        return ""
    return run.run_usage.models[0]


def cost_usd(run: TrajectoryRun) -> Optional[float]:
    if run.run_usage is None:
        return None
    return run.run_usage.cost_usd


def cost_source(run: TrajectoryRun) -> str:
    if run.run_usage is None:
        return ""
    return run.run_usage.cost_source


def total_tokens(run: TrajectoryRun) -> int:
    if run.run_usage is None:
        return 0
    return run.run_usage.total_tokens


def usage_for_turn(
    run: TrajectoryRun,
    turn: Optional[int],
) -> Optional[TurnUsageView]:
    if turn is None:
        return None
    return run._turn_map.get(turn)
