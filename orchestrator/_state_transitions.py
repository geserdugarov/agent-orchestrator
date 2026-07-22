# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Declared workflow transition graph construction."""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Optional

from orchestrator._workflow_labels import WorkflowLabel

_DETOUR_TO_RESOLVING: frozenset[WorkflowLabel] = frozenset(
    (
        WorkflowLabel.VALIDATING,
        WorkflowLabel.DOCUMENTING,
        WorkflowLabel.IN_REVIEW,
        WorkflowLabel.FIXING,
    ),
)

_FORWARD: Mapping[
    Optional[WorkflowLabel], frozenset[WorkflowLabel]
] = MappingProxyType({
    None: frozenset((WorkflowLabel.DECOMPOSING, WorkflowLabel.IMPLEMENTING)),
    WorkflowLabel.DECOMPOSING: frozenset(
        (
            WorkflowLabel.READY,
            WorkflowLabel.IMPLEMENTING,
            WorkflowLabel.BLOCKED,
            WorkflowLabel.UMBRELLA,
        ),
    ),
    WorkflowLabel.READY: frozenset(
        (WorkflowLabel.IMPLEMENTING, WorkflowLabel.DECOMPOSING),
    ),
    WorkflowLabel.BLOCKED: frozenset(
        (WorkflowLabel.READY, WorkflowLabel.DECOMPOSING),
    ),
    WorkflowLabel.UMBRELLA: frozenset(
        (WorkflowLabel.DONE, WorkflowLabel.DECOMPOSING),
    ),
    WorkflowLabel.IMPLEMENTING: frozenset((WorkflowLabel.VALIDATING,)),
    WorkflowLabel.VALIDATING: frozenset(
        (WorkflowLabel.DOCUMENTING, WorkflowLabel.FIXING),
    ),
    WorkflowLabel.DOCUMENTING: frozenset(
        (WorkflowLabel.IN_REVIEW, WorkflowLabel.VALIDATING),
    ),
    WorkflowLabel.IN_REVIEW: frozenset(
        (WorkflowLabel.FIXING, WorkflowLabel.VALIDATING),
    ),
    WorkflowLabel.FIXING: frozenset(
        (
            WorkflowLabel.VALIDATING,
            WorkflowLabel.RESOLVING_CONFLICT,
            WorkflowLabel.IN_REVIEW,
        ),
    ),
    WorkflowLabel.RESOLVING_CONFLICT: frozenset((WorkflowLabel.VALIDATING,)),
    WorkflowLabel.QUESTION: frozenset((WorkflowLabel.DONE,)),
    WorkflowLabel.DONE: frozenset(),
    WorkflowLabel.REJECTED: frozenset(),
})

_INTERRUPT_SOURCES: Mapping[
    WorkflowLabel, frozenset[WorkflowLabel]
] = MappingProxyType({
    WorkflowLabel.DONE: frozenset(
        (
            WorkflowLabel.IMPLEMENTING,
            WorkflowLabel.VALIDATING,
            WorkflowLabel.DOCUMENTING,
            WorkflowLabel.IN_REVIEW,
            WorkflowLabel.FIXING,
            WorkflowLabel.RESOLVING_CONFLICT,
        ),
    ),
    WorkflowLabel.REJECTED: frozenset(
        (
            WorkflowLabel.IMPLEMENTING,
            WorkflowLabel.VALIDATING,
            WorkflowLabel.DOCUMENTING,
            WorkflowLabel.IN_REVIEW,
            WorkflowLabel.FIXING,
            WorkflowLabel.RESOLVING_CONFLICT,
        ),
    ),
    WorkflowLabel.RESOLVING_CONFLICT: _DETOUR_TO_RESOLVING,
})


def _mutable_forward_transitions(
) -> dict[Optional[WorkflowLabel], set[WorkflowLabel]]:
    """Copy the forward graph into mutable target sets."""
    return {
        forward_source: set(forward_targets)
        for forward_source, forward_targets in _FORWARD.items()
    }


def _add_interrupt_transitions(
    allowed: dict[Optional[WorkflowLabel], set[WorkflowLabel]],
) -> None:
    """Fold each target's exact interrupt sources into the graph."""
    for target, sources in _INTERRUPT_SOURCES.items():
        for interrupt_source in sources:
            allowed[interrupt_source].add(target)


def _freeze_transitions(
    allowed: dict[Optional[WorkflowLabel], set[WorkflowLabel]],
) -> dict[Optional[WorkflowLabel], frozenset[WorkflowLabel]]:
    """Freeze target sets so the exported graph is immutable."""
    return {
        allowed_source: frozenset(edges)
        for allowed_source, edges in allowed.items()
    }


def build_allowed_transitions(
) -> dict[Optional[WorkflowLabel], frozenset[WorkflowLabel]]:
    """Compose the forward spine with exact interrupt sources."""
    allowed = _mutable_forward_transitions()
    _add_interrupt_transitions(allowed)
    return _freeze_transitions(allowed)


ALLOWED_TRANSITIONS = build_allowed_transitions()
