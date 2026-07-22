# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Parse decoded trajectory JSON objects into typed views."""

from __future__ import annotations

from typing import Any, Optional

from orchestrator import _trajectory_constants as constants
from orchestrator import _trajectory_record_values as record_values
from orchestrator._trajectory_run_model import TrajectoryRun
from orchestrator._trajectory_view_models import (
    RunUsageView,
    TrajectoryStepView,
    TurnUsageView,
)


def parse_step(raw_step: Any) -> Optional[TrajectoryStepView]:
    if not isinstance(raw_step, dict):
        return None
    kind = record_values.coerce_str(raw_step.get("kind"))
    if not kind:
        return None
    return TrajectoryStepView(
        kind=kind,
        name=record_values.coerce_str(raw_step.get("name")),
        tool_id=record_values.coerce_str(raw_step.get("tool_id")),
        content=record_values.coerce_str(raw_step.get("content")),
        turn=record_values.coerce_int(raw_step.get("turn")),
    )


def parse_run_usage(raw_usage: Any) -> Optional[RunUsageView]:
    if not isinstance(raw_usage, dict):
        return None
    return RunUsageView(
        models=record_values.coerce_str_tuple(raw_usage.get("models")),
        turns=record_values.coerce_int(raw_usage.get("turns")),
        input_tokens=record_values.coerce_int(raw_usage.get("input_tokens")) or 0,
        output_tokens=record_values.coerce_int(raw_usage.get("output_tokens")) or 0,
        cached_tokens=record_values.coerce_int(raw_usage.get("cached_tokens")) or 0,
        cache_read_tokens=record_values.coerce_int(raw_usage.get("cache_read_tokens")) or 0,
        cache_write_tokens=record_values.coerce_int(raw_usage.get("cache_write_tokens")) or 0,
        cost_usd=record_values.coerce_float(raw_usage.get("cost_usd")),
        cost_source=record_values.coerce_str(raw_usage.get("cost_source")),
    )


def parse_turn(raw_turn: Any) -> Optional[TurnUsageView]:
    if not isinstance(raw_turn, dict):
        return None
    return TurnUsageView(
        turn=record_values.coerce_int(raw_turn.get("turn")),
        model=record_values.coerce_str(raw_turn.get("model")),
        input_tokens=record_values.coerce_int(raw_turn.get("input_tokens")) or 0,
        output_tokens=record_values.coerce_int(raw_turn.get("output_tokens")) or 0,
        cache_read_tokens=record_values.coerce_int(raw_turn.get("cache_read_tokens")) or 0,
        cache_write_tokens=record_values.coerce_int(raw_turn.get("cache_write_tokens")) or 0,
        cost_usd=record_values.coerce_float(raw_turn.get("cost_usd")),
        cost_source=record_values.coerce_str(raw_turn.get("cost_source")),
    )


def parse_record(record_object: Any, *, sequence: int) -> Optional[TrajectoryRun]:
    if not isinstance(record_object, dict):
        return None
    if record_object.get("event") != constants.TRAJECTORY_EVENT:
        return None
    raw_steps = record_values.as_list(record_object.get("steps"))
    raw_turns = record_values.as_list(record_object.get("turns"))
    steps = tuple(step for step in map(parse_step, raw_steps) if step is not None)
    turns = tuple(turn for turn in map(parse_turn, raw_turns) if turn is not None)
    return TrajectoryRun(
        seq=sequence,
        ts=record_values.coerce_str(record_object.get("ts")),
        repo=record_values.coerce_str(record_object.get("repo")),
        issue=record_values.coerce_int(record_object.get("issue")) or 0,
        stage=record_values.coerce_str(record_object.get("stage")),
        agent_role=record_values.coerce_str(record_object.get("agent_role")),
        backend=record_values.coerce_str(record_object.get("backend")),
        session_id=record_values.coerce_str(record_object.get("session_id")),
        review_round=record_values.coerce_int(record_object.get("review_round")),
        retry_count=record_values.coerce_int(record_object.get("retry_count")),
        user_input=record_values.coerce_str(record_object.get("user_input")),
        system_prompt=record_values.coerce_str(record_object.get("system_prompt")),
        output=record_values.coerce_str(record_object.get("output")),
        tools=record_values.coerce_str_tuple(record_object.get("tools")),
        skills_triggered=record_values.coerce_str_tuple(record_object.get("skills_triggered")),
        skills_available=record_values.coerce_str_tuple(record_object.get("skills_available")),
        steps=steps,
        run_usage=parse_run_usage(record_object.get("run_usage")),
        turns=turns,
        truncated=bool(record_object.get("truncated")),
    )
