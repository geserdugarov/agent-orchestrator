# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory usage, turn, step, and record serialization."""

from __future__ import annotations

from typing import Any

from orchestrator import usage
from orchestrator.analytics._recording import (
    _AgentExitContext,
    _live_settings,
    build_record,
)
from orchestrator.analytics._trajectory_models import (
    _TrajectoryBudget,
    _TrajectoryHeadline,
)
from orchestrator.analytics._trajectory_sanitize import (
    _Redactor,
    _redact_and_truncate,
)


def _trajectory_usage(metrics: usage.UsageMetrics) -> dict[str, Any]:
    run_usage = metrics.to_dict()
    run_usage.pop("backend", None)
    return run_usage


def _trajectory_headline(
    context: _AgentExitContext,
    trajectory: usage.AgentTrajectory,
    metrics: usage.UsageMetrics,
    redact: _Redactor,
) -> _TrajectoryHeadline:
    return _TrajectoryHeadline(
        user_input=_redact_and_truncate(context.prompt, redact),
        system_prompt=_redact_and_truncate(trajectory.system_prompt, redact),
        output=_redact_and_truncate(trajectory.final_output, redact),
        run_usage=_trajectory_usage(metrics),
    )


def _bounded_trajectory_turns(
    trajectory: usage.AgentTrajectory,
    budget: _TrajectoryBudget,
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for turn in trajectory.turns:
        turn_dict = turn.to_dict()
        if not budget.include(turn_dict):
            break
        turns.append(turn_dict)
    return turns


def _trajectory_step(step: usage.TrajectoryStep, redact: _Redactor) -> dict[str, Any]:
    step_dict: dict[str, Any] = {
        "kind": step.kind,
        "name": step.name or None,
        "tool_id": step.tool_id or None,
        "content": _redact_and_truncate(step.content, redact),
    }
    if step.turn is not None:
        step_dict["turn"] = step.turn
    return step_dict


def _bounded_trajectory_steps(
    trajectory: usage.AgentTrajectory,
    budget: _TrajectoryBudget,
    redact: _Redactor,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if budget.truncated:
        return steps
    for step in trajectory.steps:
        step_dict = _trajectory_step(step, redact)
        if not budget.include(step_dict):
            break
        steps.append(step_dict)
    return steps


def _build_trajectory_record(
    context: _AgentExitContext,
    trajectory: usage.AgentTrajectory,
    metrics: usage.UsageMetrics,
    redact: _Redactor,
) -> dict:
    """Assemble one redacted, truncated `agent_trajectory` record.

    `prompt` becomes the redacted `user_input`; `system_prompt`, each
    step's content, and the final `output` are redacted the same way.
    `metrics` (the same `UsageMetrics` the baseline `agent_exit` record
    already carries) is denormalized into a `run_usage` summary so the
    file-only viewer needs no re-parse; it is `UsageMetrics.to_dict()` minus
    `backend` (already a record field). Its token counts / cost / model name
    are not secret-shaped, so they skip redaction. The claude per-turn
    breakdown rides along as `turns` (empty on codex, whose usage frames are
    cumulative -- `build_record` then drops the key).

    Each step is charged its full *serialized* size -- the JSON metadata
    (`kind` / `name` / `tool_id` / `turn`) plus its truncated content, not
    merely `len(content)` -- so steps with empty or tiny content still consume
    the budget. The per-turn `turns` array is charged and truncated the same
    way (a run with thousands of turns and no steps would otherwise write the
    whole array in full and blow the budget); it is drawn down before the
    steps, so once the running total crosses `_TRAJECTORY_RECORD_BUDGET` the
    remaining turns -- then steps -- are dropped and `truncated` is set. Only
    the small fixed `run_usage` summary is always kept whole. `build_record`
    drops every `None`-valued field, so an absent prompt, empty system prompt,
    no-trigger skill set, or codex's empty per-turn array leaves its key off
    rather than storing a null.
    """
    headline = _trajectory_headline(context, trajectory, metrics, redact)
    budget = _TrajectoryBudget(
        headline.serialized_size,
        _live_settings()._TRAJECTORY_RECORD_BUDGET,
    )
    turns = _bounded_trajectory_turns(trajectory, budget)
    steps = _bounded_trajectory_steps(trajectory, budget, redact)
    return build_record(
        repo=context.repo,
        issue=context.issue,
        event="agent_trajectory",
        stage=context.stage,
        agent_role=context.agent_role,
        backend=context.backend,
        session_id=context.agent_result.session_id,
        review_round=context.review_round,
        retry_count=context.retry_count,
        user_input=headline.user_input,
        system_prompt=headline.system_prompt,
        tools=list(trajectory.tools) or None,
        skills_triggered=list(trajectory.skills.triggered) or None,
        skills_available=list(trajectory.skills.available) or None,
        run_usage=headline.run_usage,
        turns=turns or None,
        steps=steps,
        output=headline.output,
        truncated=budget.truncated or None,
    )
