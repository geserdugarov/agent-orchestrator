# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Timeline, fixture, and label views for trajectory runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from orchestrator import _trajectory_constants as constants
from orchestrator._trajectory_view_models import TimelineEntry, TurnUsageView

if TYPE_CHECKING:
    from orchestrator._trajectory_run_model import TrajectoryRun


def timeline(run: TrajectoryRun) -> tuple[TimelineEntry, ...]:
    entries: list[TimelineEntry] = []
    if run.user_input:
        entries.append(
            TimelineEntry(
                kind=constants.TIMELINE_PROMPT,
                content=run.user_input,
            )
        )
    for step in run.steps:
        entries.append(
            TimelineEntry(
                kind=step.kind,
                content=step.content,
                name=step.name,
                tool_id=step.tool_id,
                turn=step.turn,
            )
        )
    if run.output:
        entries.append(
            TimelineEntry(
                kind=constants.TIMELINE_OUTPUT,
                content=run.output,
            )
        )
    return tuple(entries)


def is_fixture(run: TrajectoryRun) -> bool:
    if run.user_input.strip().lower() == constants.FIXTURE_PROMPT:
        return True
    if run.session_id.startswith(constants.FIXTURE_SESSION_PREFIX):
        return True
    skill_only = all(
        step.is_call and step.name == constants.FIXTURE_SKILL_TOOL
        for step in run.steps
    )
    if run.steps and skill_only:
        return True
    return False


def detail_label(run: TrajectoryRun) -> str:
    stage = run.stage or "—"
    role = run.agent_role or "—"
    backend = run.backend or "—"
    round_suffix = "" if run.review_round is None else f" · round {run.review_round}"
    return f"{stage}/{role} · {backend}{round_suffix} · {run.ts}"


def label(run: TrajectoryRun) -> str:
    return f"#{run.issue} {run.repo} · {run.detail_label()}"


def turn_map(run: TrajectoryRun) -> dict[int, TurnUsageView]:
    return {turn_usage.turn: turn_usage for turn_usage in run.turns if turn_usage.turn is not None}
