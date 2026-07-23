# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Codex JSON event builders for usage tests."""

from tests import usage_test_values as _usage_cases


def usage(*, input: int = 0, cached: int = 0, output: int = 0) -> dict:
    return {
        _usage_cases.INPUT_TOKENS_FIELD: input,
        "cached_input_tokens": cached,
        _usage_cases.OUTPUT_TOKENS_FIELD: output,
    }


def turn_complete(
    *,
    model: str | None = None,
    input: int = 0,
    cached: int = 0,
    output: int = 0,
) -> dict:
    frame = {_usage_cases.TYPE_FIELD: "turn_complete"}
    if model is not None:
        frame[_usage_cases.MODEL_FIELD] = model
    frame[_usage_cases.USAGE_FIELD] = usage(input=input, cached=cached, output=output)
    return frame


def task_started(**fields: object) -> dict:
    return {_usage_cases.TYPE_FIELD: "task_started", **fields}


def task_complete(**fields: object) -> dict:
    return {_usage_cases.TYPE_FIELD: "task_complete", **fields}


def command(
    item_id: str,
    command_text: str,
    *,
    started: bool = False,
    **extra_fields: object,
) -> dict:
    event_type = "item.started" if started else "item.completed"
    command_record = {
        _usage_cases.IDENTIFIER_FIELD: item_id,
        _usage_cases.TYPE_FIELD: "command_execution",
        _usage_cases.COMMAND_FIELD: command_text,
    }
    command_record.update(extra_fields)
    return {_usage_cases.TYPE_FIELD: event_type, "item": command_record}


def agent_message(item_id: str, message: object, *, started: bool = False) -> dict:
    event_type = "item.started" if started else "item.completed"
    return {
        _usage_cases.TYPE_FIELD: event_type,
        "item": {
            _usage_cases.IDENTIFIER_FIELD: item_id,
            _usage_cases.TYPE_FIELD: "agent_message",
            _usage_cases.TEXT_FIELD: message,
        },
    }
