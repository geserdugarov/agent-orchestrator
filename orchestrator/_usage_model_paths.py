# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Provider model-name extraction from nested event payloads."""

from __future__ import annotations

from typing import Any, Optional

from orchestrator import _usage_metric_protocol as protocol


CLAUDE_MODEL_PATHS: tuple[protocol.ModelPath, ...] = (
    (protocol.MESSAGE, protocol.MODEL),
    ("event", protocol.MESSAGE, protocol.MODEL),
    (protocol.MODEL,),
    ("response", protocol.MODEL),
)

CODEX_MODEL_PATHS: tuple[protocol.ModelPath, ...] = (
    (protocol.MODEL,),
    ("response", protocol.MODEL),
    (protocol.ITEM_KEY, protocol.MODEL),
    ("event", protocol.MODEL),
    (protocol.PAYLOAD, protocol.MODEL),
    (protocol.PAYLOAD, "settings", protocol.MODEL),
    (protocol.PAYLOAD, "collaboration_mode", "settings", protocol.MODEL),
    (protocol.INFO_KEY, protocol.MODEL),
    (protocol.PAYLOAD, protocol.INFO_KEY, protocol.MODEL),
)


def nested_value(payload: dict[str, Any], path: protocol.ModelPath) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def known_model(candidate: Any) -> Optional[str]:
    if isinstance(candidate, str) and candidate and candidate != protocol.UNKNOWN:
        return candidate
    return None


def nonempty_string(candidate: Any) -> Optional[str]:
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


def first_model_at_paths(
    event: dict[str, Any],
    paths: tuple[protocol.ModelPath, ...],
) -> Optional[str]:
    for path in paths:
        model = known_model(nested_value(event, path))
        if model is not None:
            return model
    return None


def first_string_at_paths(
    event: dict[str, Any],
    paths: tuple[protocol.ModelPath, ...],
) -> Optional[str]:
    for path in paths:
        text = nonempty_string(nested_value(event, path))
        if text is not None:
            return text
    return None


def claude_model_name(event: dict[str, Any]) -> str:
    return first_string_at_paths(event, CLAUDE_MODEL_PATHS) or protocol.UNKNOWN


def codex_model_name(
    event: dict[str, Any],
    usage: Optional[dict[str, Any]],
) -> str:
    event_model = first_model_at_paths(event, CODEX_MODEL_PATHS)
    if event_model is not None:
        return event_model
    usage_model = known_model(usage.get(protocol.MODEL)) if usage else None
    return usage_model or protocol.UNKNOWN
