# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Recursive redaction and bounded trajectory text formatting."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from orchestrator.analytics._recording import _live_settings

_Redactor = Callable[[str], str]


def _truncate_head_tail(text: str, head: int, tail: int) -> str:
    """Keep the first `head` + last `tail` chars of `text`, eliding the
    middle with a marker recording how many chars were dropped. Returns
    `text` unchanged when it already fits within `head + tail`."""
    if len(text) <= head + tail:
        return text
    elided = len(text) - head - tail
    head_text = text[:head]
    tail_text = text[-tail:]
    return f"{head_text}\n...[{elided} chars elided]...\n{tail_text}"


def _redact_tree(node: Any, redact: _Redactor) -> Any:
    r"""Recursively redact every string leaf of a tool payload.

    Applied before JSON serialization so a multiline / control-character
    secret in a tool input or result is masked on the raw leaf:
    `json.dumps` would otherwise escape its newlines (a real `\n` becomes
    the two-character `\n` escape), leaving `_redact_secrets`' literal
    `str.replace` unable to match the raw env value -- and the secret would
    survive into `steps[].content`. Dict keys are structural field names and
    pass through unredacted; only values and list elements carry
    agent-sourced content. Non-string scalars (numbers, bools, `None`) are
    returned as-is.
    """
    if isinstance(node, str):
        return redact(node)
    if isinstance(node, dict):
        return {key: _redact_tree(child, redact) for key, child in node.items()}
    if isinstance(node, list):
        return [_redact_tree(child, redact) for child in node]
    return node


def _redact_and_truncate(field_value: Any, redact: _Redactor) -> Optional[str]:
    """Redact then per-field head/tail truncate one trajectory value.

    String leaves are redacted with `_redact_secrets` BEFORE any JSON
    serialization. A plain string is redacted directly; dict / list content
    (claude tool inputs are dicts; `tool_result` content a list) is redacted
    leaf-by-leaf via `_redact_tree` first, then serialized -- serializing
    first would escape a multiline secret's newlines so the redactor's
    literal `str.replace` could no longer match it. A final redact pass over
    the serialized text is a cheap safety net for any leaf the walk could
    not reach (e.g. a value stringified by `default=str`). Redaction precedes
    truncation so a secret spanning the elided middle cannot leak as two
    halves. Empty / `None` content yields `None` so `build_record` drops the
    field rather than storing an empty string.
    """
    if field_value is None:
        return None
    if isinstance(field_value, str):
        text = redact(field_value)
    else:
        try:
            text = json.dumps(
                _redact_tree(field_value, redact),
                sort_keys=True,
                default=str,
            )
        except (TypeError, ValueError):
            text = str(_redact_tree(field_value, redact))
        text = redact(text)
    if not text:
        return None
    settings = _live_settings()
    return _truncate_head_tail(
        text,
        settings._TRAJECTORY_FIELD_HEAD,
        settings._TRAJECTORY_FIELD_TAIL,
    )
