# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Coordinating trajectory-run record with delegated computed views."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Optional

from orchestrator import _trajectory_run_timeline as run_timeline
from orchestrator import _trajectory_run_views as run_views
from orchestrator._trajectory_view_models import (
    RunUsageView,
    TrajectoryStepView,
    TurnUsageView,
)


@dataclass(frozen=True)
class TrajectoryRun:
    """One parsed and normalized agent-trajectory record."""

    seq: int
    ts: str
    repo: str
    issue: int
    stage: str = ""
    agent_role: str = ""
    backend: str = ""
    session_id: str = ""
    review_round: Optional[int] = None
    retry_count: Optional[int] = None
    user_input: str = ""
    system_prompt: str = ""
    output: str = ""
    tools: tuple[str, ...] = ()
    skills_triggered: tuple[str, ...] = ()
    skills_available: tuple[str, ...] = ()
    steps: tuple[TrajectoryStepView, ...] = ()
    run_usage: Optional[RunUsageView] = None
    turns: tuple[TurnUsageView, ...] = ()
    truncated: bool = False

    tool_calls = property(run_views.tool_calls)
    step_count = property(run_views.step_count)
    model = property(run_views.model)
    cost_usd = property(run_views.cost_usd)
    cost_source = property(run_views.cost_source)
    total_tokens = property(run_views.total_tokens)
    usage_for_turn = run_views.usage_for_turn
    timeline = property(run_timeline.timeline)
    is_fixture = property(run_timeline.is_fixture)
    detail_label = run_timeline.detail_label
    label = run_timeline.label
    _turn_map = cached_property(run_timeline.turn_map)


TrajectoryRun.__module__ = "orchestrator._trajectory_records"
