# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Reconstruct ordered Codex command and message trajectory steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from orchestrator import _usage_metric_protocol as protocol
from orchestrator import _usage_skill_codex as skill_codex
from orchestrator._usage_trajectory_models import TrajectoryStep


TEXT = "text"
TOOL_RESULT = "tool_result"
MISSING = object()


def final_output(events: Iterable[dict[str, Any]]) -> Optional[str]:
    final_text: Optional[str] = None
    for event in events:
        stream_item = event.get(protocol.ITEM_KEY)
        if not isinstance(stream_item, dict):
            continue
        if stream_item.get(protocol.TYPE) != "agent_message":
            continue
        candidate = stream_item.get(TEXT)
        if isinstance(candidate, str):
            final_text = candidate
    return final_text


def trajectory_steps(
    events: Iterable[dict[str, Any]],
) -> tuple[TrajectoryStep, ...]:
    builder = CodexTrajectoryBuilder()
    for event in events:
        builder.add_event(event)
    return builder.build()


@dataclass
class CodexTrajectoryBuilder:
    order: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    commands: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    anonymous: list[TrajectoryStep] = field(default_factory=list)

    def add_event(self, event: dict[str, Any]) -> None:
        stream_item = event.get(protocol.ITEM_KEY)
        if not isinstance(stream_item, dict):
            return
        item_id = self._item_id(stream_item)
        if stream_item.get(protocol.TYPE) == skill_codex.COMMAND_EXECUTION:
            self._add_command(stream_item, item_id)
        elif stream_item.get(protocol.TYPE) == "agent_message":
            self._add_message(stream_item, item_id)

    def build(self) -> tuple[TrajectoryStep, ...]:
        step_groups = CodexStepGroups(
            order=self.order,
            commands=self.commands,
            outputs=self.outputs,
            messages=self.messages,
            anonymous=self.anonymous,
        )
        return assemble_steps(step_groups)

    def _item_id(self, stream_item: dict[str, Any]) -> str:
        raw_id = stream_item.get(protocol.ID)
        item_id = raw_id if isinstance(raw_id, str) and raw_id else ""
        if item_id and item_id not in self.seen:
            self.seen.add(item_id)
            self.order.append(item_id)
        return item_id

    def _add_command(self, stream_item: dict[str, Any], item_id: str) -> None:
        command = stream_item.get("command")
        has_output = "aggregated_output" in stream_item
        if item_id:
            if isinstance(command, str):
                self.commands[item_id] = command
            if has_output:
                self.outputs[item_id] = stream_item.get("aggregated_output")
            return
        if isinstance(command, str):
            self.anonymous.append(
                TrajectoryStep(
                    kind="tool_call",
                    name=skill_codex.COMMAND_EXECUTION,
                    content=command,
                )
            )
        if has_output:
            self.anonymous.append(
                TrajectoryStep(
                    kind=TOOL_RESULT,
                    content=stream_item.get("aggregated_output"),
                )
            )

    def _add_message(self, stream_item: dict[str, Any], item_id: str) -> None:
        message = stream_item.get(TEXT)
        if not isinstance(message, str) or not message:
            return
        if item_id:
            self.messages[item_id] = message
            return
        self.anonymous.append(
            TrajectoryStep(
                kind="assistant_message",
                content=message,
            )
        )


@dataclass(frozen=True)
class CodexStepGroups:
    order: list[str]
    commands: dict[str, str]
    outputs: dict[str, Any]
    messages: dict[str, str]
    anonymous: list[TrajectoryStep]


def assemble_steps(groups: CodexStepGroups) -> tuple[TrajectoryStep, ...]:
    steps: list[TrajectoryStep] = []
    for item_id in groups.order:
        command = groups.commands.get(item_id, MISSING)
        if command is not MISSING:
            steps.append(
                TrajectoryStep(
                    kind="tool_call",
                    name=skill_codex.COMMAND_EXECUTION,
                    tool_id=item_id,
                    content=command,
                )
            )
        output = groups.outputs.get(item_id, MISSING)
        if output is not MISSING:
            steps.append(
                TrajectoryStep(
                    kind=TOOL_RESULT,
                    tool_id=item_id,
                    content=output,
                )
            )
        message = groups.messages.get(item_id, MISSING)
        if message is not MISSING:
            steps.append(
                TrajectoryStep(
                    kind="assistant_message",
                    content=message,
                )
            )
    steps.extend(groups.anonymous)
    return tuple(steps)
