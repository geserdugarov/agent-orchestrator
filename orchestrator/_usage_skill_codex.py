# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Collect de-duplicated Codex skill command observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator import _usage_metric_protocol as protocol
from orchestrator import _usage_skill_commands as commands


COMMAND_EXECUTION = "command_execution"
CommandSkillNames = tuple[list[str], list[str]]


@dataclass
class CodexSkillCollector:
    by_id: dict[str, CommandSkillNames] = field(default_factory=dict)
    id_order: list[str] = field(default_factory=list)
    anon_inferred: list[str] = field(default_factory=list)
    anon_incidental: list[str] = field(default_factory=list)

    def add_event(self, event: dict[str, Any]) -> None:
        stream_item = event.get(protocol.ITEM_KEY)
        if not isinstance(stream_item, dict):
            return
        if stream_item.get(protocol.TYPE) != COMMAND_EXECUTION:
            return
        command = stream_item.get("command")
        if not isinstance(command, str):
            return
        inferred, incidental = commands.classify_codex_command(command)
        if not inferred and not incidental:
            return
        item_id = stream_item.get(protocol.ID)
        if isinstance(item_id, str) and item_id:
            if item_id not in self.by_id:
                self.id_order.append(item_id)
            self.by_id[item_id] = (inferred, incidental)
            return
        self.anon_inferred.extend(inferred)
        self.anon_incidental.extend(incidental)

    def inferred_names(self) -> list[str]:
        return self._ordered(0) + self.anon_inferred

    def incidental_names(self) -> list[str]:
        return self._ordered(1) + self.anon_incidental

    def _ordered(self, bucket: int) -> list[str]:
        return [name for item_id in self.id_order for name in self.by_id[item_id][bucket]]
