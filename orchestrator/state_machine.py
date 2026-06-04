# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed workflow states for the orchestrator's label-based state machine.

`WorkflowLabel` is the single source of truth for the workflow label
vocabulary. It is a `StrEnum`, so every member *is* its on-the-wire
string: existing string comparisons (`label == "validating"`), JSON
serialization of pinned state, and PyGithub label writes keep working
unchanged. The point of the enum is not to replace the strings but to
give them one authoritative definition, IDE/refactor support, and a
membership set the typo guard can validate against.

`ControlLabel` holds operator-applied *modifiers* (`backlog`,
`hold_base_sync`) that coexist with a workflow label and pause/redirect
processing without being workflow states themselves -- an issue is
`implementing` + `backlog` at once. They are deliberately NOT workflow
states and never flow through `set_workflow_label` or the transition
table.

The transition table and guard live in this module too (added alongside
`validate_transition`); `github.set_workflow_label` is the single
chokepoint that calls them.
"""
from __future__ import annotations

import logging
from enum import StrEnum
from typing import Optional

log = logging.getLogger(__name__)


class WorkflowLabel(StrEnum):
    """The workflow states. Member value == the GitHub label string."""

    DECOMPOSING = "decomposing"
    READY = "ready"
    BLOCKED = "blocked"
    UMBRELLA = "umbrella"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    DOCUMENTING = "documenting"
    IN_REVIEW = "in_review"
    FIXING = "fixing"
    RESOLVING_CONFLICT = "resolving_conflict"
    QUESTION = "question"
    DONE = "done"
    REJECTED = "rejected"


class ControlLabel(StrEnum):
    """Operator-applied modifiers that coexist with a workflow label.

    Not workflow states: they gate or redirect processing while leaving
    the underlying `WorkflowLabel` intact (a child can be `ready` +
    `backlog` -- "ready in the FSM, but operator-held"). Never passed to
    `set_workflow_label` and never present in the transition table.
    """

    BACKLOG = "backlog"
    HOLD_BASE_SYNC = "hold_base_sync"


def coerce_workflow_label(value: str) -> WorkflowLabel:
    """Return the `WorkflowLabel` for ``value`` or raise ``ValueError``.

    Called at every orchestrator-authored workflow-label write
    (`set_workflow_label`, `create_child_issue`) so a typo'd label name
    fails loudly instead of being applied as a literal GitHub label and
    then silently demoted to unlabeled-pickup on the next tick (a label
    not in `WorkflowLabel` is invisible to `workflow_label`).

    Accepts an existing `WorkflowLabel` (idempotent) or its string value.
    """
    try:
        return WorkflowLabel(value)
    except ValueError:
        valid = ", ".join(repr(str(m)) for m in WorkflowLabel)
        raise ValueError(
            f"{value!r} is not a valid workflow label; expected one of: {valid}"
        ) from None


class IllegalTransition(Exception):
    """A workflow-label write would make a transition absent from
    ``ALLOWED_TRANSITIONS``. Raised only in ``enforce`` guard mode."""


# Terminal states have no outgoing edges.
# The per-tick base-sync detour relabels a behind-base PR-having issue to
# `resolving_conflict`. These are the ONLY states it fires from. Enumerated
# explicitly here (rather than imported from `base_sync`) so the table is
# self-describing; `tests/test_state_machine.py` asserts it stays equal to
# `base_sync._PR_REFRESH_DETOUR_LABELS` so the two cannot drift apart.
_DETOUR_TO_RESOLVING: frozenset[WorkflowLabel] = frozenset(
    {
        WorkflowLabel.VALIDATING, WorkflowLabel.DOCUMENTING,
        WorkflowLabel.IN_REVIEW, WorkflowLabel.FIXING,
    }
)

# Forward ("spine") + drift edges, keyed by source. ``None`` is the entry
# (unlabeled-pickup) pseudo-state. The interrupt / detour edges
# (`-> done`, `-> rejected`, `-> resolving_conflict`) are folded in below by
# `_build_allowed` from `_INTERRUPT_SOURCES`, so this map holds only the
# deterministic forward flow (plus `umbrella`/`question` -> `done`, which is
# those states' own forward completion rather than an external interrupt).
_FORWARD: dict[Optional[WorkflowLabel], frozenset[WorkflowLabel]] = {
    # Entry: an unlabeled issue decomposes, or (DECOMPOSE=off) goes straight
    # to implementing. It never enters `question` (operator-applied only) and
    # is never born `blocked` via this path -- children are created `blocked`
    # directly, bypassing the transition guard.
    None: frozenset({WorkflowLabel.DECOMPOSING, WorkflowLabel.IMPLEMENTING}),
    WorkflowLabel.DECOMPOSING: frozenset(
        {
            WorkflowLabel.READY, WorkflowLabel.IMPLEMENTING,
            WorkflowLabel.BLOCKED, WorkflowLabel.UMBRELLA,
        }
    ),
    # `-> decomposing` on each of ready/blocked/umbrella is the user-content
    # drift re-route (`_route_drift_to_decomposing`).
    WorkflowLabel.READY: frozenset(
        {WorkflowLabel.IMPLEMENTING, WorkflowLabel.DECOMPOSING}
    ),
    WorkflowLabel.BLOCKED: frozenset(
        {WorkflowLabel.READY, WorkflowLabel.DECOMPOSING}
    ),
    WorkflowLabel.UMBRELLA: frozenset(
        {WorkflowLabel.DONE, WorkflowLabel.DECOMPOSING}
    ),
    WorkflowLabel.IMPLEMENTING: frozenset({WorkflowLabel.VALIDATING}),
    WorkflowLabel.VALIDATING: frozenset({WorkflowLabel.DOCUMENTING}),
    WorkflowLabel.DOCUMENTING: frozenset(
        {WorkflowLabel.IN_REVIEW, WorkflowLabel.VALIDATING}
    ),
    WorkflowLabel.IN_REVIEW: frozenset(
        {WorkflowLabel.FIXING, WorkflowLabel.VALIDATING}
    ),
    WorkflowLabel.FIXING: frozenset({WorkflowLabel.VALIDATING}),
    WorkflowLabel.RESOLVING_CONFLICT: frozenset({WorkflowLabel.VALIDATING}),
    WorkflowLabel.QUESTION: frozenset({WorkflowLabel.DONE}),
    WorkflowLabel.DONE: frozenset(),
    WorkflowLabel.REJECTED: frozenset(),
}


# Interrupt / detour edges, keyed by TARGET -> the EXACT set of source states
# whose handlers (or the helpers they call) actually emit that target. Modeled
# per-target rather than "any non-terminal" so the guard is maximally exact: a
# pre-PR state (`decomposing` / `ready` / `blocked`) is never terminalized,
# and `question` only finalizes to `done`, never `rejected`.
#
#  * -> done     : external merge mid-stage (`_finalize_if_pr_merged`, called
#                  from implementing / validating / documenting entry checks
#                  and from blocked/umbrella merged-child recovery -- the child
#                  always carries a PR-having stage label) and the review-side
#                  terminal drain (`_drain_review_pr_terminals`). `umbrella` /
#                  `question` reach `done` via their own forward edge, above.
#  * -> rejected : PR / issue closed without merge
#                  (`_finalize_if_issue_closed`, `_drain_review_pr_terminals`).
#  * -> resolving_conflict : the per-tick base-sync detour.
_INTERRUPT_SOURCES: dict[WorkflowLabel, frozenset[WorkflowLabel]] = {
    WorkflowLabel.DONE: frozenset(
        {
            WorkflowLabel.IMPLEMENTING, WorkflowLabel.VALIDATING,
            WorkflowLabel.DOCUMENTING, WorkflowLabel.IN_REVIEW,
            WorkflowLabel.FIXING, WorkflowLabel.RESOLVING_CONFLICT,
        }
    ),
    WorkflowLabel.REJECTED: frozenset(
        {
            WorkflowLabel.IMPLEMENTING, WorkflowLabel.VALIDATING,
            WorkflowLabel.DOCUMENTING, WorkflowLabel.IN_REVIEW,
            WorkflowLabel.FIXING, WorkflowLabel.RESOLVING_CONFLICT,
        }
    ),
    WorkflowLabel.RESOLVING_CONFLICT: _DETOUR_TO_RESOLVING,
}


def _build_allowed() -> dict[Optional[WorkflowLabel], frozenset[WorkflowLabel]]:
    """Compose the forward spine with the per-target interrupt sources.

    `_FORWARD` supplies the deterministic forward edges; each
    `_INTERRUPT_SOURCES[target]` then adds `target` to exactly the sources
    that emit it. Terminal states appear only as targets (never as keys with
    outgoing edges), and the entry pseudo-state (`None`) gets no interrupt
    edge -- an unlabeled issue is never terminalized directly.
    """
    allowed: dict[Optional[WorkflowLabel], set[WorkflowLabel]] = {
        src: set(forward) for src, forward in _FORWARD.items()
    }
    for target, sources in _INTERRUPT_SOURCES.items():
        for src in sources:
            allowed[src].add(target)
    return {src: frozenset(edges) for src, edges in allowed.items()}


ALLOWED_TRANSITIONS: dict[Optional[WorkflowLabel], frozenset[WorkflowLabel]] = (
    _build_allowed()
)


def is_allowed_transition(
    current: Optional[WorkflowLabel], new: WorkflowLabel
) -> bool:
    """True if relabeling ``current`` -> ``new`` is legal.

    A same-label write (idempotent re-set) is always allowed; it still
    fires `set_labels` / `stage_enter` exactly as before -- the guard does
    not suppress those.
    """
    if current == new:
        return True
    return new in ALLOWED_TRANSITIONS.get(current, frozenset())


def guard_transition(
    current: Optional[WorkflowLabel], new: WorkflowLabel, mode: str
) -> None:
    """Apply the configured transition guard at a workflow-label write.

    ``mode`` is the ``WORKFLOW_TRANSITION_GUARD`` setting:
    * ``off``     -- no check.
    * ``warn``    -- log a warning on an illegal transition, then proceed.
    * ``enforce`` -- raise ``IllegalTransition`` on an illegal transition.

    The typo guard (`coerce_workflow_label`) is independent of this and is
    always strict; this only governs transition *legality*.
    """
    if mode == "off" or is_allowed_transition(current, new):
        return
    allowed = ", ".join(
        sorted(str(s) for s in ALLOWED_TRANSITIONS.get(current, frozenset()))
    )
    detail = (
        f"illegal workflow transition "
        f"{str(current) if current is not None else None!r} -> {str(new)!r}; "
        f"allowed from there: {allowed or '(none -- terminal state)'}"
    )
    if mode == "enforce":
        raise IllegalTransition(detail)
    log.warning("%s", detail)
