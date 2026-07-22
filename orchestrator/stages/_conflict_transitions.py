# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Conflict transitions."""
from __future__ import annotations

from orchestrator.stages import _conflict_state as _state
from orchestrator.stages import conflicts as _owner

_ConflictContext = _owner._ConflictContext
Optional = _owner.Optional
WorkflowLabel = _owner.WorkflowLabel
_CONFLICT_ROUND = _state._CONFLICT_ROUND
_REVIEW_ROUND = _state._REVIEW_ROUND


def _park_conflict(ctx: _ConflictContext, message: str, *, reason: str) -> None:
    """Park awaiting human and persist pinned state.

    Every `resolving_conflict` park pairs `_park_awaiting_human` with the
    matching `write_pinned_state`; routing them through here keeps the two
    from drifting apart across the handler's many exits.
    """
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(ctx.gh, ctx.issue, ctx.state, message, reason=reason)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _emit_conflict_round_incremented(
    ctx: _ConflictContext,
    *,
    pr_number: int,
    new_round: int,
    outcome: str,
    sha: Optional[str] = None,
) -> None:
    """Record a `conflict_round` audit event when the counter ticks.

    Centralizes the bookkeeping so every increment site -- ahead-of-remote
    push recovery, up-to-date no-op flip, clean base-rebase push, agent-
    resolved conflict push, drift-pushed bounce -- emits the same shape.
    `outcome` distinguishes the increment cause so a tail of the JSONL sink
    can attribute rounds without re-reading the surrounding code.
    """
    ctx.gh.emit_event(
        _CONFLICT_ROUND,
        issue_number=ctx.issue.number,
        stage="resolving_conflict",
        pr_number=int(pr_number),
        sha=sha or None,
        action="incremented",
        conflict_round=int(new_round),
        outcome=outcome,
        review_round=int(ctx.state.get(_REVIEW_ROUND) or 0),
        retry_count=ctx.state.get("retry_count"),
    )


def _hand_resolved_round_to_validating(
    ctx: _ConflictContext,
    conflict_round: int,
    pr_number,
    *,
    outcome: str,
    sha: Optional[str],
) -> None:
    """Record a pushed conflict-resolution round and hand back to `validating`.

    Resets `review_round` (rebasing rewrites SHAs, so validation must
    re-approve the rebased branch), bumps `conflict_round`, stamps
    `last_conflict_resolved_at`, emits the `conflict_round` audit event, flips
    the label, and persists pinned state. Shared by every pushed-diff exit --
    recovered push, clean base rebase, agent resolution, and the drift resume.
    Docs do not run here: the single docs pass is deferred to the post-approval
    handoff to `documenting` in `_handle_validating`.
    """
    from orchestrator import workflow as _wf

    ctx.state.set(_REVIEW_ROUND, 0)
    ctx.state.set(_CONFLICT_ROUND, conflict_round + 1)
    ctx.state.set("last_conflict_resolved_at", _wf._now_iso())
    _owner._emit_conflict_round_incremented(
        ctx,
        pr_number=int(pr_number),
        new_round=conflict_round + 1,
        outcome=outcome,
        sha=sha,
    )
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)
