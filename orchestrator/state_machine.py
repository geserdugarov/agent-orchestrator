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

`ControlLabel` holds operator-applied *modifiers* (`backlog`, `paused`,
`community_contribution`, `quick_run`) that coexist with a workflow label or
PR and pause/redirect/modify processing without being workflow states
themselves -- an issue is `implementing` + `backlog` at once. They are
deliberately NOT workflow states and never flow through `set_workflow_label`
or the transition table.

The transition table and guard live in this module too; `github.set_workflow_label`
is the single chokepoint that calls them.
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

    `backlog` and `paused` are both hard skips -- the orchestrator ignores
    the issue entirely while either is present. They differ only in intent:
    `backlog` is a "not yet" hold on a fresh issue, `paused` an operator
    pause on an in-flight one.

    `quick_run` is deliberately NOT a hard skip: it stays attached and
    modifies the normal workflow rather than pausing it, so the orchestrator
    keeps processing the issue while the label is present.
    """

    BACKLOG = "backlog"
    PAUSED = "paused"
    COMMUNITY_CONTRIBUTION = "community_contribution"
    QUICK_RUN = "quick_run"


def coerce_workflow_label(value: str) -> WorkflowLabel:
    """Return the `WorkflowLabel` for ``value`` or raise ``ValueError``.

    The typo guard for orchestrator-authored label writes: `set_workflow_label`
    calls it directly, and `create_child_issue` reaches it through
    `coerce_child_issue_label` (which first admits the propagable `quick_run`
    modifier). A typo'd label name fails loudly instead of being applied as a
    literal GitHub label and then silently demoted to unlabeled-pickup on the
    next tick (a label not in `WorkflowLabel` is invisible to `workflow_label`).

    Accepts an existing `WorkflowLabel` (idempotent) or its string value.
    """
    try:
        return WorkflowLabel(value)
    except ValueError:
        valid = ", ".join(repr(str(m)) for m in WorkflowLabel)
        raise ValueError(
            f"{value!r} is not a valid workflow label; expected one of: {valid}"
        ) from None


# Control labels a freshly-created child issue may carry at birth alongside
# its initial workflow label. `quick_run` is the only one: a split parent
# carrying it propagates the modifier to every child so the accelerated mode
# survives decomposition. `backlog` / `paused` / `community_contribution` are
# operator- or PR-applied and are never seeded at child creation.
_CREATABLE_CONTROL_LABELS: frozenset[ControlLabel] = frozenset(
    {ControlLabel.QUICK_RUN}
)


def coerce_child_issue_label(value: str) -> str:
    """Return the validated label string for a `create_child_issue` write.

    A child is born with an initial `WorkflowLabel`, and may additionally
    carry a control label in `_CREATABLE_CONTROL_LABELS` (currently only
    `quick_run`, propagated from a split parent). Anything that is not a
    propagable modifier is validated as a workflow label through
    `coerce_workflow_label` (the single typo-guard source of truth), so an
    unknown workflow label, a misspelling, or a control label never seeded at
    creation (`backlog` / `paused` / `community_contribution`) fails loudly
    here instead of becoming an invisible literal GitHub label.
    """
    try:
        control = ControlLabel(value)
    except ValueError:
        control = None
    if control in _CREATABLE_CONTROL_LABELS:
        return control
    try:
        return coerce_workflow_label(value)
    except ValueError:
        accepted = (*WorkflowLabel, *_CREATABLE_CONTROL_LABELS)
        valid = ", ".join(repr(str(m)) for m in accepted)
        raise ValueError(
            f"{value!r} is not a valid child-issue label; "
            f"expected one of: {valid}"
        ) from None


class IllegalTransition(Exception):
    """A workflow-label write would make a transition absent from
    ``ALLOWED_TRANSITIONS``. Raised only in ``enforce`` guard mode."""


# Terminal states have no outgoing edges.
# The per-tick base-sync detour relabels a PR-having issue to
# `resolving_conflict` only when the refresh-time rebase leaves conflicted
# files; clean behind-base rebases route straight to `validating`. These are
# the ONLY states the conflict detour fires from. Enumerated explicitly here
# (rather than imported from `base_sync`) so the table is self-describing;
# `tests/test_state_machine.py` asserts it stays equal to
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
    # `-> in_review` is the `quick_run` fast path: a clean developer result on
    # a `quick_run`-labeled issue publishes its PR and routes straight to
    # `in_review`, bypassing the reviewer (`validating`) and docs
    # (`documenting`) passes. An ordinary issue takes `-> validating`.
    WorkflowLabel.IMPLEMENTING: frozenset(
        {WorkflowLabel.VALIDATING, WorkflowLabel.IN_REVIEW}
    ),
    WorkflowLabel.VALIDATING: frozenset(
        {WorkflowLabel.DOCUMENTING, WorkflowLabel.FIXING}
    ),
    WorkflowLabel.DOCUMENTING: frozenset(
        {WorkflowLabel.IN_REVIEW, WorkflowLabel.VALIDATING}
    ),
    WorkflowLabel.IN_REVIEW: frozenset(
        {WorkflowLabel.FIXING, WorkflowLabel.VALIDATING}
    ),
    WorkflowLabel.FIXING: frozenset(
        {
            WorkflowLabel.VALIDATING,
            # The worktree-drift dead-lock breaker hands a stuck
            # validating-route transient park to `resolving_conflict`
            # when the worktree is out of sync with the PR head.
            WorkflowLabel.RESOLVING_CONFLICT,
            # The ACK fast path returns an in_review-route resume that
            # explicitly marked its no-commit reply with `ACK: <reason>`
            # straight to `in_review` without parking.
            WorkflowLabel.IN_REVIEW,
        }
    ),
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
#  * -> resolving_conflict : the per-tick base-sync conflict detour.
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


def _mutable_forward_transitions(
) -> dict[Optional[WorkflowLabel], set[WorkflowLabel]]:
    """Copy the declared forward graph into mutable target sets."""
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
    """Freeze target sets so the exported transition table is immutable."""
    return {
        allowed_source: frozenset(edges)
        for allowed_source, edges in allowed.items()
    }


def _build_allowed() -> dict[Optional[WorkflowLabel], frozenset[WorkflowLabel]]:
    """Compose the forward spine with the per-target interrupt sources.

    `_FORWARD` supplies the deterministic forward edges; each
    `_INTERRUPT_SOURCES[target]` then adds `target` to exactly the sources
    that emit it. Terminal states appear only as targets (never as keys with
    outgoing edges), and the entry pseudo-state (`None`) gets no interrupt
    edge -- an unlabeled issue is never terminalized directly.
    """
    allowed = _mutable_forward_transitions()
    _add_interrupt_transitions(allowed)
    return _freeze_transitions(allowed)


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
