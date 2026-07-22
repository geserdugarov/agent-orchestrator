# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Backend-agnostic session identifier extraction from JSONL."""
from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PRIORITY_KEYS = ("session_id", "conversation_id", "thread_id", "session", "id")


def _first_nested_uuid(payload_nodes: Iterator[Any]) -> Optional[str]:
    for payload_node in payload_nodes:
        found_uuid = _walk_for_uuid(payload_node)
        if found_uuid is not None:
            return found_uuid
    return None


def _walk_mapping_for_uuid(payload_node: dict[Any, Any]) -> Optional[str]:
    priority_values = (
        payload_node[key]
        for key in _PRIORITY_KEYS
        if key in payload_node
    )
    priority_match = _first_nested_uuid(priority_values)
    if priority_match is not None:
        return priority_match
    return _first_nested_uuid(iter(payload_node.values()))


def _walk_for_uuid(payload_node: Any) -> Optional[str]:
    if isinstance(payload_node, str):
        return payload_node if _UUID_RE.match(payload_node) else None
    if isinstance(payload_node, dict):
        return _walk_mapping_for_uuid(payload_node)
    if isinstance(payload_node, list):
        return _first_nested_uuid(iter(payload_node))
    return None


def parse_session_id(jsonl_output: str) -> Optional[str]:
    """Return the first UUID at a known key anywhere in JSONL events."""
    for raw_line in jsonl_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event_payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = _walk_for_uuid(event_payload)
        if session_id:
            return session_id
    return None
