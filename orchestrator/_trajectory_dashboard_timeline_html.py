# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory timeline entry HTML and turn association."""
from __future__ import annotations

import html
from types import MappingProxyType
from typing import Mapping, Optional

from orchestrator import trajectory_reader


TimelineUsagePair = tuple[
    Optional[trajectory_reader.TurnUsageView],
    trajectory_reader.TimelineEntry,
]
BADGE_BY_KIND: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        trajectory_reader.TIMELINE_PROMPT: ("prompt", "prompt"),
        trajectory_reader.TIMELINE_OUTPUT: ("output", "final output"),
        "tool_call": ("call", "tool call"),
        "tool_result": ("result", "tool result"),
        "assistant_message": ("assistant", "assistant"),
        "user_message": ("user", "user turn"),
    }
)


def _timeline_entry_html(
    entry: trajectory_reader.TimelineEntry,
    index: int,
) -> str:
    """Render one typed timeline entry."""
    badge_class, badge_text = BADGE_BY_KIND.get(
        entry.kind,
        ("result", entry.kind or "step"),
    )
    name_html = (
        f'<span class="orch-traj-step-name">{html.escape(entry.name)}</span>'
        if entry.name
        else ""
    )
    identifier_html = (
        f'<span class="orch-traj-step-id">{html.escape(entry.tool_id)}</span>'
        if entry.tool_id
        else ""
    )
    step_number = index + 1
    return (
        '<div class="orch-traj-step">'
        f'<span class="orch-traj-step-idx">{step_number}</span>'
        f'<span class="orch-traj-badge {badge_class}">'
        f"{html.escape(badge_text)}</span>{name_html}{identifier_html}</div>"
    )


def _timeline_with_usage(
    run: trajectory_reader.TrajectoryRun,
) -> list[TimelineUsagePair]:
    """Pair the first entry of each assistant turn with its usage strip."""
    paired: list[TimelineUsagePair] = []
    previous_turn: Optional[int] = None
    for entry in run.timeline:
        strip = None
        if entry.turn is not None and entry.turn != previous_turn:
            strip = run.usage_for_turn(entry.turn)
            previous_turn = entry.turn
        paired.append((strip, entry))
    return paired
