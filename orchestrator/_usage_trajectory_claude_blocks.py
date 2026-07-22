# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Convert Claude content blocks into trajectory steps."""

from __future__ import annotations

from typing import Any, Optional

from orchestrator import _usage_metric_protocol as protocol
from orchestrator import _usage_skill_claude as skill_claude
from orchestrator._usage_trajectory_models import TrajectoryStep


TEXT = "text"
TOOL_RESULT = "tool_result"


def assistant_steps(
    blocks: list[Any],
    turn: Optional[int],
    seen_calls: set[str],
) -> list[TrajectoryStep]:
    steps: list[TrajectoryStep] = []
    for block in blocks:
        step = assistant_step(block, turn, seen_calls)
        if step is not None:
            steps.append(step)
    return steps


def assistant_step(
    block: Any,
    turn: Optional[int],
    seen_calls: set[str],
) -> Optional[TrajectoryStep]:
    if not isinstance(block, dict):
        return None
    if block.get(protocol.TYPE) == TEXT:
        return message_step(block, "assistant_message", turn=turn)
    if block.get(protocol.TYPE) == "tool_use":
        return tool_call_step(block, turn, seen_calls)
    return None


def message_step(
    block: dict[str, Any],
    kind: str,
    *,
    turn: Optional[int] = None,
) -> Optional[TrajectoryStep]:
    message = block.get(TEXT)
    if not isinstance(message, str) or not message:
        return None
    return TrajectoryStep(kind=kind, turn=turn, content=message)


def tool_call_step(
    block: dict[str, Any],
    turn: Optional[int],
    seen_calls: set[str],
) -> Optional[TrajectoryStep]:
    name = block.get("name")
    if not isinstance(name, str) or not name:
        return None
    block_id = block.get(protocol.ID)
    tool_id = block_id if isinstance(block_id, str) and block_id else ""
    if tool_id in seen_calls:
        return None
    if tool_id:
        seen_calls.add(tool_id)
    return TrajectoryStep(
        kind="tool_call",
        name=name,
        tool_id=tool_id,
        turn=turn,
        content=block.get(protocol.INPUT),
    )


def user_steps(
    blocks: list[Any],
    seen_results: set[str],
) -> list[TrajectoryStep]:
    steps: list[TrajectoryStep] = []
    for block in blocks:
        step = user_step(block, seen_results)
        if step is not None:
            steps.append(step)
    return steps


def user_step(
    block: Any,
    seen_results: set[str],
) -> Optional[TrajectoryStep]:
    if not isinstance(block, dict):
        return None
    if block.get(protocol.TYPE) == TEXT:
        return message_step(block, "user_message")
    if block.get(protocol.TYPE) == TOOL_RESULT:
        return tool_result_step(block, seen_results)
    return None


def tool_result_step(
    block: dict[str, Any],
    seen_results: set[str],
) -> Optional[TrajectoryStep]:
    result_id = block.get("tool_use_id")
    tool_id = result_id if isinstance(result_id, str) and result_id else ""
    if tool_id in seen_results:
        return None
    if tool_id:
        seen_results.add(tool_id)
    return TrajectoryStep(
        kind=TOOL_RESULT,
        tool_id=tool_id,
        content=block.get(skill_claude.CONTENT_KEY),
    )
