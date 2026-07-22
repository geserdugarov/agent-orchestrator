# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Claude usage-frame decoding and last-frame selection."""

from __future__ import annotations

from typing import Any, Optional

from orchestrator import _usage_event_stream as event_stream
from orchestrator import _usage_metric_protocol as protocol
from orchestrator import _usage_model_paths as model_paths


ClaudeUsageRow = tuple[int, str, protocol.TokenBucket]


def claude_usage_record(usage: dict[str, Any]) -> protocol.TokenBucket:
    flat_cache_write = usage.get("cache_creation_input_tokens")
    if flat_cache_write is None:
        cache_creation = usage.get("cache_creation")
        cache_map = cache_creation if isinstance(cache_creation, dict) else {}
        cache_write_five_min = event_stream.token_count(
            cache_map.get("ephemeral_5m_input_tokens") or usage.get("ephemeral_5m_input_tokens"),
        )
        cache_write_one_hour = event_stream.token_count(
            cache_map.get("ephemeral_1h_input_tokens") or usage.get("ephemeral_1h_input_tokens"),
        )
    else:
        cache_write_five_min = event_stream.token_count(flat_cache_write)
        cache_write_one_hour = 0
    return {
        protocol.INPUT: event_stream.token_count(
            usage.get(protocol.INPUT_TOKENS) or usage.get("prompt_tokens"),
        ),
        protocol.CACHE_WRITE_FIVE_MIN: cache_write_five_min,
        protocol.CACHE_WRITE_ONE_HOUR: cache_write_one_hour,
        protocol.CACHE_READ: event_stream.token_count(
            usage.get("cache_read_input_tokens") or usage.get("cached_input_tokens") or usage.get("cache_read_tokens"),
        ),
        protocol.OUTPUT: event_stream.token_count(
            usage.get(protocol.OUTPUT_TOKENS) or usage.get("completion_tokens"),
        ),
    }


def claude_assistant_usage_row(
    index: int,
    event: dict[str, Any],
) -> Optional[tuple[str, ClaudeUsageRow]]:
    if event.get(protocol.TYPE) != protocol.ASSISTANT:
        return None
    message = event.get(protocol.MESSAGE)
    if not isinstance(message, dict):
        return None
    usage = message.get(protocol.USAGE)
    if not isinstance(usage, dict):
        return None
    message_id = message.get(protocol.ID) or event.get("request_id")
    if not message_id:
        message_id = str(index)
    return (
        str(message_id),
        (index, model_paths.claude_model_name(event), claude_usage_record(usage)),
    )


def claude_result_usage_row(
    index: int,
    event: dict[str, Any],
) -> Optional[ClaudeUsageRow]:
    if event.get(protocol.TYPE) != protocol.RESULT_KEY:
        return None
    usage = event.get(protocol.USAGE)
    if not isinstance(usage, dict):
        return None
    return index, model_paths.claude_model_name(event), claude_usage_record(usage)


def claude_usage_records(
    events: list[dict[str, Any]],
) -> list[ClaudeUsageRow]:
    by_id: dict[str, ClaudeUsageRow] = {}
    for index, event in enumerate(events):
        identified = claude_assistant_usage_row(index, event)
        if identified is not None:
            by_id[identified[0]] = identified[1]
    if by_id:
        return sorted_claude_usage_rows(by_id)
    return claude_result_usage_records(events)


def sorted_claude_usage_rows(
    by_id: dict[str, ClaudeUsageRow],
) -> list[ClaudeUsageRow]:
    usage_rows = list(by_id.values())
    usage_rows.sort(key=claude_usage_row_index)
    return usage_rows


def claude_usage_row_index(usage_row: ClaudeUsageRow) -> int:
    return usage_row[0]


def claude_result_usage_records(
    events: list[dict[str, Any]],
) -> list[ClaudeUsageRow]:
    usage_rows: list[ClaudeUsageRow] = []
    for index, event in enumerate(events):
        usage_row = claude_result_usage_row(index, event)
        if usage_row is not None:
            usage_rows.append(usage_row)
    return usage_rows
