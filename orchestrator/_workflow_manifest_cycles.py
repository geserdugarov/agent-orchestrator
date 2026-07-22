# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow manifest cycles."""
from __future__ import annotations

from orchestrator import workflow_messages as _owner


def _dep_cycle_visit(
    child_index: int, children: list[dict], color: list[int],
) -> bool:
    """DFS one node of the children dep graph; True on a back-edge to a node
    still on the stack.

    `color` is mutated in place (0=unvisited, 1=on-stack, 2=finished) and
    shared across the whole walk, so a node finished on one root is never
    re-descended from another.
    """
    color[child_index] = 1
    for dependency_index in (children[child_index].get("depends_on") or []):
        if color[dependency_index] == 1:
            return True
        if color[dependency_index] == 0 and _owner._dep_cycle_visit(
            dependency_index, children, color,
        ):
            return True
    color[child_index] = 2
    return False


def _has_dep_cycle(children: list[dict]) -> bool:
    """DFS for back-edges in the children dep graph (white/gray/black)."""
    color = [0 for _ in children]  # 0=unvisited, 1=on-stack, 2=finished
    return any(
        color[child_index] == 0
        and _owner._dep_cycle_visit(child_index, children, color)
        for child_index in range(len(children))
    )
