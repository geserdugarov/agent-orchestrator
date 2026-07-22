# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fixing parked."""
from __future__ import annotations

from orchestrator.stages import _fixing_state as _state
from orchestrator.stages import fixing as _owner

_FixingContext = _owner._FixingContext
_FixingFeedback = _owner._FixingFeedback
_ParkedFixingDecision = _owner._ParkedFixingDecision
Optional = _owner.Optional
WorkflowLabel = _owner.WorkflowLabel
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_PARK_REASON = _state._PARK_REASON
_PENDING_FIX_AT = _state._PENDING_FIX_AT


def _dispatch_continue_command(
    ctx: _FixingContext, feedback: _FixingFeedback,
) -> Optional[_ParkedFixingDecision]:
    """Apply a `/orchestrator continue` command to a parked tick.

    Returns a `_ParkedFixingDecision` when the command was resolved (a refused
    content-free continue -> `stop=True`; an accepted replay -> `stop=False`
    with the preserved batch), or ``None`` for the "passthrough" case (the
    command arrived WITH genuine guidance on a park with no replayable batch),
    where the caller falls through to the validating-recovery / normal-resume
    path so that guidance drives the dev.
    """
    action, replay_items = _owner._handle_continue_command(ctx, feedback)
    if action == "refuse":
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)
        return _ParkedFixingDecision(stop=True)
    if action == "replay":
        return _ParkedFixingDecision(stop=False, replay_batch=replay_items)
    return None


def _dispatch_validating_recovery(
    ctx: _FixingContext, feedback: _FixingFeedback, park_reason,
) -> Optional[_ParkedFixingDecision]:
    """Attempt silent recovery of a validating-route transient park.

    Returns a stop-decision when this branch owns the tick (a stuck transient
    rerouted to `resolving_conflict` on drift, or a resolved transient flipped
    back to `validating`), or ``None`` to fall through to the stay-parked /
    clear-park default.

    Only fires when the park reason can resolve without a human comment AND the
    issue arrived via the validating route (CHANGES_REQUESTED dev fix). The
    `_handle_validating` CHANGES_REQUESTED branch flips to `fixing` BEFORE
    spawning the dev, so a transient park (`push_failed` / `agent_timeout`)
    lands under `fixing` instead of `validating`; without this branch the issue
    would sit forever awaiting a human comment the underlying condition does
    not produce. Recovery must NOT run on the in_review route: that route
    advances the PR-feedback watermarks past the human comment even on a
    timed-out resume, and the shared helper bumps `review_round` on its
    `pushed` outcome, which the in_review route resets to 0 -- so a deferred
    push there would consume feedback without a fix and mis-account the round.
    The route discriminator is `pending_fix_at` (set by the in_review route,
    unset by the validating route).
    """
    from orchestrator import workflow as _wf

    validating_routed = ctx.state.get(_PENDING_FIX_AT) is None
    if (
        feedback.all_items
        or park_reason not in _wf._VALIDATING_TRANSIENT_PARK_REASONS
        or not validating_routed
    ):
        return None

    recovery = _wf._try_recover_validating_transient_park(
        ctx.spec, ctx.issue, ctx.state,
    )
    if recovery == "stuck":
        # The transient condition has not resolved on its own (e.g.
        # `push_failed` keeps failing). When the worktree has drifted from
        # the PR head in the meantime, hand the reconciliation to
        # `resolving_conflict` rather than sit parked forever -- the per-tick
        # base sync deliberately stands down on every `awaiting_human` park,
        # so nobody else will sync this worktree. Limiting the drift route to
        # this branch keeps the HITL contract intact: question / dirty /
        # silent / in_review-route transient parks fall through to the bare
        # stay-parked return below and keep waiting for a human comment.
        _owner._reconcile_parked_fixing(ctx)
        return _ParkedFixingDecision(stop=True)

    # Conditions resolved (either no fix landed or a deferred push finished).
    # Clear the park flags and flip back to `validating` so the reviewer
    # re-evaluates the current head next tick. The helper has already bumped
    # `review_round` when a fix landed (push_failed, or agent_timeout that
    # finished its push). Clear the pending_fix_* bookmarks defensively: this
    # branch ONLY fires when `pending_fix_at` was already None, so the clear is
    # a no-op in normal flow, but a stale bookmark from an earlier route would
    # otherwise mis-flag the next reviewer round.
    ctx.state.set(_AWAITING_HUMAN, False)
    ctx.state.set(_PARK_REASON, None)
    _owner._clear_pending_fix_bookmarks(ctx.state)
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)
    return _ParkedFixingDecision(stop=True)


def _dispatch_parked_fixing(
    ctx: _FixingContext, feedback: _FixingFeedback,
) -> _ParkedFixingDecision:
    """Reconcile a `fixing` tick that arrived with `awaiting_human` set.

    Returns a decision object. ``stop=True`` means the tick is fully handled
    and the caller must return immediately (auto-rebase park, a refused
    `/orchestrator continue`, a silent validating-route recovery, a
    worktree-drift reroute, or a stay-parked-until-fresh-reply). ``stop=False``
    clears the park and the caller proceeds to the resume; `replay_batch` is
    the preserved feedback batch when an accepted `/orchestrator continue`
    replays it, otherwise ``None``.
    """
    from orchestrator import workflow as _wf

    park_reason = ctx.state.get(_PARK_REASON)
    # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to the
    # `_sync_pr_worktree_to_base` retry loop -- the operator's new comment is
    # the "retry the rebase" signal, NOT fresh PR feedback for the dev
    # fix-loop. Stay silent so the refresh keeps ownership of the comment;
    # resuming the dev here would spawn it on a prompt that has nothing to do
    # with the outstanding fix.
    if park_reason in _wf._AUTO_REBASE_PARK_REASONS:
        return _ParkedFixingDecision(stop=True)

    # `/orchestrator continue` operator command (exact line, so a comment
    # carrying the command AND real guidance still counts). Handled on BOTH
    # routes so a session-failure park (`agent_silent` / `agent_timeout`) never
    # resumes the dev on the bare command text. A "replay" or "refuse"
    # decision owns the tick; a "passthrough" returns None and falls through.
    if _wf._parse_orchestrator_continue(feedback.issue_space):
        decision = _owner._dispatch_continue_command(ctx, feedback)
        if decision is not None:
            return decision

    recovery = _owner._dispatch_validating_recovery(ctx, feedback, park_reason)
    if recovery is not None:
        return recovery

    if not feedback.all_items:
        # All other awaiting_human shapes (question parks, dirty worktree
        # parks, silent-crash parks, in_review-route transients) stay parked
        # until a fresh human reply lands. We cannot distinguish "agent has a
        # real question" from "agent reported nothing to change" by inspection
        # -- both surface through `_on_question` with `park_reason=None` -- so
        # auto-routing either would silently bypass the HITL contract. The same
        # applies to a clean in-sync worktree on the in_review route: the dev
        # may have replied with a real question that needs a human to resolve,
        # so the only automatic exit from `fixing` for the in_review route is
        # the ACK fast path in the resume tail (on the same tick the dev
        # explicitly marks its no-commit reply with `ACK:`).
        return _ParkedFixingDecision(stop=True)

    ctx.state.set(_AWAITING_HUMAN, False)
    ctx.state.set(_PARK_REASON, None)
    return _ParkedFixingDecision(stop=False)
