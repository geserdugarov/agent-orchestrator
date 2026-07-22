# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Codex cumulative usage-frame decoding and model selection."""

from __future__ import annotations

from typing import Any, Optional

from orchestrator import _usage_event_stream as event_stream
from orchestrator import _usage_metric_protocol as protocol
from orchestrator import _usage_model_paths as model_paths


CODEX_USAGE_PATHS: tuple[tuple[str, ...], ...] = (
    (protocol.USAGE,),
    ("token_usage",),
    (protocol.TOTAL_TOKEN_USAGE,),
    (protocol.INFO_KEY, protocol.TOTAL_TOKEN_USAGE),
    (protocol.INFO_KEY, protocol.USAGE),
    (protocol.PAYLOAD, protocol.USAGE),
    (protocol.PAYLOAD, "token_usage"),
    (protocol.PAYLOAD, protocol.TOTAL_TOKEN_USAGE),
    (protocol.PAYLOAD, protocol.INFO_KEY, protocol.TOTAL_TOKEN_USAGE),
    (protocol.PAYLOAD, protocol.INFO_KEY, protocol.USAGE),
)


def codex_usage_block(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    for path in CODEX_USAGE_PATHS:
        current: Any = event
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, dict):
            return current
    return None


def nested_usage_field(
    usage: dict[str, Any],
    outer_key: str,
    inner_key: str,
) -> Any:
    outer = usage.get(outer_key)
    return outer.get(inner_key) if isinstance(outer, dict) else None


def codex_usage_record(usage: dict[str, Any]) -> protocol.TokenBucket:
    input_tokens = event_stream.token_count(
        usage.get(protocol.INPUT_TOKENS) or usage.get("prompt_tokens") or usage.get("total_input_tokens"),
    )
    cached_tokens = event_stream.token_count(
        usage.get("cached_input_tokens")
        or usage.get(protocol.CACHED_TOKENS)
        or nested_usage_field(usage, "input_tokens_details", protocol.CACHED_TOKENS)
        or nested_usage_field(usage, "prompt_tokens_details", protocol.CACHED_TOKENS),
    )
    output_tokens = event_stream.token_count(
        usage.get(protocol.OUTPUT_TOKENS) or usage.get("completion_tokens") or usage.get("total_output_tokens"),
    )
    return {
        protocol.INPUT: input_tokens,
        protocol.CACHED: cached_tokens,
        protocol.OUTPUT: output_tokens,
    }


def codex_usage_events(
    events: list[dict[str, Any]],
) -> list[protocol.CodexUsageEvent]:
    usage_events: list[protocol.CodexUsageEvent] = []
    for event in events:
        usage = codex_usage_block(event)
        if usage is None:
            continue
        record = codex_usage_record(usage)
        if sum(record.values()) == 0:
            continue
        usage_events.append((model_paths.codex_model_name(event, usage), record))
    return usage_events


def codex_select_model(
    events: list[dict[str, Any]],
    last_model: str,
    fallback_model: Optional[str],
) -> Optional[str]:
    chosen_model = model_paths.known_model(last_model)
    if chosen_model is not None:
        return chosen_model
    stream_model = last_stream_model(events)
    if stream_model is not None:
        return stream_model
    return model_paths.known_model(fallback_model)


def last_stream_model(events: list[dict[str, Any]]) -> Optional[str]:
    last_model: Optional[str] = None
    for event in events:
        for payload in event_stream.walk_objects(event):
            model = model_paths.known_model(payload.get(protocol.MODEL))
            if model is not None:
                last_model = model
    return last_model


def last_codex_usage(
    usage_events: list[protocol.CodexUsageEvent],
) -> protocol.CodexUsageEvent:
    if usage_events:
        return usage_events[-1]
    return protocol.UNKNOWN, {
        protocol.INPUT: 0,
        protocol.CACHED: 0,
        protocol.OUTPUT: 0,
    }
