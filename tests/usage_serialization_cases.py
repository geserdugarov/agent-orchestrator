# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Serializable usage model cases and payload projections."""

import json

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases


def build_usage_metrics() -> _usage.UsageMetrics:
    return _usage.UsageMetrics(
        backend=_usage_cases.CODEX,
        models=(_usage_cases.GPT_FIVE_CODEX,),
        turns=3,
        input_tokens=100,
        output_tokens=_usage_cases.TOKEN_COUNT_FIFTY,
        cached_tokens=10,
        cost_usd=_usage_cases.SERIALIZED_USAGE_COST_USD,
        cost_source=_usage_cases.ESTIMATED_COST_SOURCE,
    )


def build_agent_trajectory() -> _usage.AgentTrajectory:
    return _usage.AgentTrajectory(
        backend=_usage_cases.CLAUDE,
        tools=(_usage_cases.BASH_TOOL, _usage_cases.READ_TOOL),
        skills=_usage.SkillTriggers(
            triggered=_usage_cases.DEVELOP_ONLY,
            trigger_counts=_usage_cases.DEVELOP_TRIGGER_COUNTS,
            available=(_usage_cases.DEVELOP, _usage_cases.REVIEW),
        ),
        steps=(
            _usage.TrajectoryStep(
                kind=_usage_cases.TOOL_CALL_STEP,
                name=_usage_cases.BASH_TOOL,
                tool_id=_usage_cases.TASK_ONE_ID,
                turn=0,
                content={_usage_cases.COMMAND_FIELD: _usage_cases.LIST_COMMAND},
            ),
            _usage.TrajectoryStep(
                kind=_usage_cases.TOOL_RESULT_STEP,
                tool_id=_usage_cases.TASK_ONE_ID,
                content="out",
            ),
        ),
        final_output=_usage_cases.FINAL_OUTPUT,
        turns=(
            _usage.TurnUsage(
                turn=0,
                model=_usage_cases.OPUS_FOUR_EIGHT,
                input_tokens=_usage_cases.CLAUDE_TURN_INPUT_TOKENS,
                output_tokens=_usage_cases.CLAUDE_TURN_OUTPUT_TOKENS,
                cache_read_tokens=_usage_cases.CLAUDE_TURN_CACHE_READ_TOKENS,
                cache_write_tokens=_usage_cases.CLAUDE_TURN_CACHE_WRITE_TOKENS,
                cost_usd=_usage_cases.SERIALIZED_TRAJECTORY_COST_USD,
                cost_source=_usage_cases.ESTIMATED_COST_SOURCE,
            ),
        ),
    )


def serialize(record) -> dict:
    return json.loads(json.dumps(record.to_dict(), sort_keys=True))


def trajectory_summary(payload: dict) -> tuple:
    return (
        payload["backend"],
        payload["tools"],
        (payload["system_prompt"], payload["final_output"]),
        (payload["skills"]["triggered"], payload["skills"]["available"]),
    )


def trajectory_steps(payload: dict) -> tuple:
    steps = payload[_usage_cases.STEPS_FIELD]
    return (
        len(steps),
        (steps[0][_usage_cases.NAME_FIELD], steps[0]["turn"]),
        (steps[1]["kind"], steps[1]["turn"]),
    )


def trajectory_turns(payload: dict) -> tuple:
    turns = payload[_usage_cases.TURNS_FIELD]
    return (
        len(turns),
        (
            turns[0][_usage_cases.MODEL_FIELD],
            turns[0]["cache_read_tokens"],
            turns[0]["cost_source"],
        ),
    )
