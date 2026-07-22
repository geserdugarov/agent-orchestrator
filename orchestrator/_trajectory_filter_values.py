# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory filter-option collection and free-text matching."""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from orchestrator._trajectory_run_model import TrajectoryRun


def distinct_sorted(
    runs: Sequence[TrajectoryRun],
    key: Callable[[TrajectoryRun], str],
) -> tuple[str, ...]:
    collected: set[str] = set()
    for run in runs:
        field_value = key(run)
        if field_value:
            collected.add(field_value)
    return tuple(sorted(collected))


def matches_query(run: TrajectoryRun, needle: str) -> bool:
    searchable_text: list[str] = [
        run.repo,
        run.stage,
        run.agent_role,
        run.user_input,
        run.system_prompt,
        run.output,
    ]
    searchable_text.extend(run.tools)
    searchable_text.extend(run.skills_triggered)
    searchable_text.extend(run.skills_available)
    for step in run.steps:
        searchable_text.append(step.name)
        searchable_text.append(step.content)
    return any(needle in text.lower() for text in searchable_text if text)


def normalize_filter_values(
    selected_values: Optional[Sequence[str]],
) -> Optional[frozenset[str]]:
    return frozenset(selected_values) if selected_values else None


def normalize_filter_query(query: Optional[str]) -> Optional[str]:
    if query is None:
        return None
    normalized_query = query.strip().lower()
    return normalized_query or None
