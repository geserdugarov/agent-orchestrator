# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Claude skill-tool and init-frame extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from orchestrator import _usage_metric_protocol as protocol


CONTENT_KEY = "content"


def claude_skill_name(block: Any) -> Optional[str]:
    if not isinstance(block, dict):
        return None
    is_tool_use = block.get(protocol.TYPE) == "tool_use"
    if not is_tool_use or block.get("name") != "Skill":
        return None
    tool_input = block.get(protocol.INPUT)
    if not isinstance(tool_input, dict):
        return None
    skill = tool_input.get("skill")
    if isinstance(skill, str) and skill:
        return skill
    return None


def claude_init_field(
    events: Iterable[dict[str, Any]],
    field_name: str,
) -> Any:
    for event in events:
        if event.get(protocol.TYPE) != "system":
            continue
        if event.get("subtype") != "init":
            continue
        return event.get(field_name)
    return None


def ordered_unique_names(raw_names: Any) -> tuple[str, ...]:
    if not isinstance(raw_names, list):
        return ()
    ordered_names: list[str] = []
    seen_names: set[str] = set()
    for name in raw_names:
        if not isinstance(name, str) or not name or name in seen_names:
            continue
        seen_names.add(name)
        ordered_names.append(name)
    return tuple(ordered_names)


def claude_offered_skills(
    events: Iterable[dict[str, Any]],
) -> tuple[str, ...]:
    return ordered_unique_names(claude_init_field(events, "skills"))


@dataclass
class ClaudeSkillCollector:
    names: list[str] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)

    def add_event(self, event: dict[str, Any]) -> None:
        if event.get(protocol.TYPE) != protocol.ASSISTANT:
            return
        message = event.get(protocol.MESSAGE)
        if not isinstance(message, dict):
            return
        blocks = message.get(CONTENT_KEY)
        if not isinstance(blocks, list):
            return
        for block in blocks:
            self._add_block(block)

    def _add_block(self, block: Any) -> None:
        name = claude_skill_name(block)
        if name is None:
            return
        block_id = block.get(protocol.ID)
        if isinstance(block_id, str) and block_id:
            if block_id in self.seen_ids:
                return
            self.seen_ids.add(block_id)
        self.names.append(name)
