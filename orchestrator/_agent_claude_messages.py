# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Final-message extraction from Claude stream-json output."""
from __future__ import annotations

import json
from typing import Any, Iterator, Optional


def _decode_claude_event(raw_line: str) -> Optional[dict[str, Any]]:
    """Decode one stream event, ignoring blank or diagnostic output."""
    line = raw_line.strip()
    if not line:
        return None
    try:
        event_payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event_payload if isinstance(event_payload, dict) else None


def _iter_claude_events(jsonl_output: str) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from Claude's mixed JSONL output."""
    for raw_line in jsonl_output.splitlines():
        event_payload = _decode_claude_event(raw_line)
        if event_payload is not None:
            yield event_payload


def _collect_claude_text_blocks(
    content_blocks: list[Any],
) -> Optional[str]:
    """Join valid text blocks from one assistant message."""
    text_blocks: list[str] = []
    for content_block in content_blocks:
        if not isinstance(content_block, dict):
            continue
        if content_block.get("type") != "text":
            continue
        block_text = content_block.get("text")
        if isinstance(block_text, str):
            text_blocks.append(block_text)
    return "".join(text_blocks) if text_blocks else None


def _claude_result_text(
    event_payload: dict[str, Any],
) -> Optional[str]:
    """Return a terminal result string without filtering its subtype."""
    if event_payload.get("type") != "result":
        return None
    result_text = event_payload.get("result")
    return result_text if isinstance(result_text, str) else None


def _claude_assistant_text(
    event_payload: dict[str, Any],
) -> Optional[str]:
    """Return text from a supported assistant or message event."""
    if event_payload.get("type") not in ("assistant", "message"):
        return None
    nested_message = event_payload.get("message")
    message_payload = (
        nested_message if isinstance(nested_message, dict) else event_payload
    )
    message_content = message_payload.get("content")
    if isinstance(message_content, list):
        return _collect_claude_text_blocks(message_content)
    return message_content if isinstance(message_content, str) else None


def _collect_claude_message_candidates(
    events: Iterator[dict[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    """Keep the latest terminal and assistant message candidates."""
    last_result: Optional[str] = None
    last_assistant_text: Optional[str] = None
    for event_payload in events:
        result_text = _claude_result_text(event_payload)
        if result_text is not None:
            last_result = result_text
        assistant_text = _claude_assistant_text(event_payload)
        if assistant_text is not None:
            last_assistant_text = assistant_text
    return last_result, last_assistant_text


def claude_last_message(
    jsonl_output: str,
    *,
    allow_assistant_fallback: bool = True,
) -> str:
    """Prefer terminal output and optionally fall back to assistant text."""
    candidates = _collect_claude_message_candidates(
        _iter_claude_events(jsonl_output),
    )
    last_result, last_assistant_text = candidates
    if last_result is not None:
        return last_result
    if allow_assistant_fallback:
        return last_assistant_text or ""
    return ""
