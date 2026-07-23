# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Claude stream-json event builders for usage tests."""

from dataclasses import dataclass

from tests import usage_jsonl_helpers as _jsonl
from tests import usage_test_values as _usage_cases


@dataclass(frozen=True)
class ClaudeUsagePayload:
    input: int = 0
    output: int = 0
    cache_write: int | None = None
    cache_read: int | None = None
    cache_five_minute: int | None = None
    cache_one_hour: int | None = None


def usage(**fields: int | None) -> dict:
    payload = ClaudeUsagePayload(**fields)
    usage_block = {
        _usage_cases.INPUT_TOKENS_FIELD: payload.input,
        _usage_cases.OUTPUT_TOKENS_FIELD: payload.output,
    }
    if payload.cache_write is not None:
        usage_block["cache_creation_input_tokens"] = payload.cache_write
    if payload.cache_read is not None:
        usage_block["cache_read_input_tokens"] = payload.cache_read
    if payload.cache_five_minute is not None or payload.cache_one_hour is not None:
        usage_block["cache_creation"] = {
            "ephemeral_5m_input_tokens": payload.cache_five_minute or 0,
            "ephemeral_1h_input_tokens": payload.cache_one_hour or 0,
        }
    return usage_block


def assistant(
    *,
    id: str = "msg_1",
    model: str | None = None,
    usage: dict | None = None,
    content_blocks: list | None = None,
) -> dict:
    message = {_usage_cases.IDENTIFIER_FIELD: id}
    if model is not None:
        message[_usage_cases.MODEL_FIELD] = model
    if content_blocks is not None:
        message["content"] = content_blocks
    if usage is not None:
        message[_usage_cases.USAGE_FIELD] = usage
    return {_usage_cases.TYPE_FIELD: "assistant", "message": message}


def system_init(**fields: object) -> dict:
    return {_usage_cases.TYPE_FIELD: "system", "subtype": "init", **fields}


def terminal_result(**fields: object) -> dict:
    return {_usage_cases.TYPE_FIELD: "result", **fields}


def skill_use(
    skill: str,
    *,
    id: str | None = None,
    args: object = None,
) -> dict:
    payload = {"skill": skill}
    if args is not None:
        payload["args"] = args
    return _jsonl.tool_use(_usage_cases.SKILL_TOOL, payload, id=id)
