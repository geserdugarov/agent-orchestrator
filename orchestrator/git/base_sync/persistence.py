# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Durable writes, notices, and audit events a recovered rebase leaves behind.

The park side and the finalize side live together because both publish the
same surfaces in the same order: the comment, then the audit event, then --
for a finalize that routes -- the relabel, and only last the
`write_pinned_state` the whole sequence commits through. Every field either
one sets before that write is staged in memory, so a tick that dies partway
leaves the recovery anchor pinned and the next tick re-derives the same
outcome from it instead of resuming a half-finished one. Both sides hinge on
that anchor: parking clears it after resetting HEAD back onto it, finalizing
clears it once the rewrite is confirmed published.
"""
from __future__ import annotations

from github.Issue import Issue

from orchestrator.git import commands
from orchestrator.git.base_sync.models import (
    _AutoRebaseContext,
    _AutoRebaseRecoveryContext,
)
from orchestrator.git.base_sync.state import (
    _AUTO_REBASE_PARK_REASONS,
    _AWAITING_HUMAN,
    _PARK_REASON,
    _PENDING_ANNOUNCED_SHA,
    _PENDING_PUSH_SHA,
    _PENDING_REWRITE_PR,
    _PENDING_REWRITE_SHA,
    _PENDING_REWRITE_STAGE,
    _REVIEW_ROUND,
    log,
)
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.state import WorkflowLabel, stage_name

# Everything one auto-rebase attempt puts on the pinned comment, so the step
# that ends it drops the whole record rather than the field it happens to
# name.
_ATTEMPT_KEYS = (
    _PENDING_PUSH_SHA,
    _PENDING_REWRITE_SHA,
    _PENDING_REWRITE_PR,
    _PENDING_REWRITE_STAGE,
    _PENDING_ANNOUNCED_SHA,
)


def _clears_the_attempt(state: PinnedState) -> None:
    """Drop the whole record of one auto-rebase attempt.

    The anchor, the head the replay produced, and the publication it was
    produced for are one record and are dropped as one: the anchor alone would
    bring a later tick back to an attempt it cannot prove the checkout belongs
    to, and the rest names a commit and a pull request nothing is leased to
    publish onto. Every step that ends an attempt -- the reset that puts the
    branch back, the no-op that moved nothing, the relabel that takes the issue
    out of the refresh's reach, and the finalize that publishes -- goes through
    here rather than spelling one field, so a road that forgets a member cannot
    exist.
    """
    for key in _ATTEMPT_KEYS:
        state.set(key, None)


def _park_auto_rebase_failure(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    message: str,
    reason: str,
) -> None:
    """Park an issue awaiting human for an auto-rebase failure.

    Wraps `_park_awaiting_human` so every refresh-time failure mode
    parks identically: `awaiting_human=True`, the HITL message lands
    on the issue thread (NOT the PR -- the resume-on-human-reply
    scan reads from the issue), `last_action_comment_id` is ratcheted
    forward by `_park_awaiting_human`, and the durable
    `park_reason` is re-set after the helper clears it by contract.
    `gh.write_pinned_state` is called here so the caller can return
    immediately.

    `reason` must be one of `_AUTO_REBASE_PARK_REASONS` -- the refresh
    recovery branch keys off the same set to decide whether a new
    human comment on this issue is the "retry now" signal.
    """
    # Lazy import: the guard owner sits in the workflow layer above this
    # package, and that layer imports this package at module load time, so
    # binding it here at module load would be circular.
    from orchestrator.workflow.engine import guards as _guards
    assert reason in _AUTO_REBASE_PARK_REASONS, (
        f"_park_auto_rebase_failure called with reason={reason!r}, "
        f"which is not in _AUTO_REBASE_PARK_REASONS"
    )
    _guards._park_awaiting_human(gh, issue, state, message, reason=reason)
    state.set(_PARK_REASON, reason)
    gh.write_pinned_state(issue, state)


def _reset_clear_and_park(
    context: _AutoRebaseContext | _AutoRebaseRecoveryContext,
    reset_sha: str,
    *,
    message: str,
    reason: str,
    clean: bool = False,
) -> None:
    """Restore the worktree to `reset_sha`, drop the recovery anchor, and park.

    The shared tail of every auto-rebase park path: a rebase / push /
    recovery step could not safely finalize, so HEAD is hard-reset back
    to a known SHA (the pre-rebase anchor = the last-known remote PR
    head) so the same-tick stage handler dispatch never reads a local
    HEAD the PR may not carry, the crash-recovery anchor is cleared (the
    reset put HEAD back at it, so a follow-up tick would only hit the
    "HEAD == anchor" no-op case), and the issue is parked awaiting human.
    `clean=True` also runs `git clean -fd` after the reset to discard the
    untracked leftovers a dirty rebase produced (recoverable via
    `git reflog`).

    A failed reset / clean is logged but does not abort the park: the
    `awaiting_human` flag is what short-circuits the same-tick handlers,
    and it still lands even if the worktree is left on an unexpected SHA
    for the operator to inspect.

    The debt the reset abandoned is dropped with the anchor, and only once
    the reset has actually LANDED. The size gate measures a rebased head
    before it is pushed and, at or under the ceiling, records it as a commit
    still owed a publication -- and a reset that landed puts the branch back
    on the pre-rebase SHA, so that commit is not on this branch any more and
    only the reflog still has it. Left standing there, it is a debt nothing
    can pay and everything trips over: the pre-tick base refresh freezes this
    branch out of the sync for as long as the issue lives, and the
    reconciliation ahead of every handler stops the tick for a publication
    that is never coming. An approval whose commit was abandoned is
    superseded, which has always been one of the three things that drops one
    -- so the owner doing the abandoning is the one that drops it.

    A reset that FAILED abandoned nothing, and the record is the only thing
    naming what the checkout may still be standing on: the approved commit,
    the head its push is pinned to, and the route bookkeeping that push
    closes. Dropped there, the exact-candidate retry has nothing to ask for
    by id and the next tick measures whatever the worktree turns out to be.
    So the two are ordered -- the reset is proved first, and the record
    follows it rather than the intent.

    The permission a transfer granted goes in the same write and for the same
    reason: a rebase of a commit an adjudication accepted may be licensed to
    carry that verdict over, and the reset puts the branch back onto the
    commit the exemption never left. The exemption itself needs no repair --
    the grant moved nothing -- so what is left over is a claim about a push
    that will never happen, for an object no branch has any more. Only an
    outstanding record this build can read back whole is dropped, which is the
    rollback's own rule wherever a rewrite is undone.
    """
    reset = commands._git_hardened(
        "reset", "--hard", reset_sha, cwd=context.worktree,
    )
    restored = reset.returncode == 0
    if not restored:
        log.error(
            "issue=#%d auto-rebase recovery: reset --hard to %s failed: "
            "%s; the awaiting_human park still short-circuits same-tick "
            "handler dispatch but operator inspection of HEAD is needed",
            context.issue.number,
            reset_sha[:8],
            (reset.stderr or "").strip(),
        )
    if clean:
        cleaned = commands._git_hardened("clean", "-fd", cwd=context.worktree)
        if cleaned.returncode != 0:
            log.error(
                "issue=#%d auto-rebase recovery: `git clean -fd` after "
                "the reset failed: %s",
                context.issue.number, (cleaned.stderr or "").strip(),
            )
    _clears_the_attempt(context.state)
    if restored:
        _forgets_the_reset(context, reset_sha)
    _park_auto_rebase_failure(
        context.gh,
        context.issue,
        context.state,
        message=message,
        reason=reason,
    )


def _forgets_the_reset(
    context: _AutoRebaseContext | _AutoRebaseRecoveryContext,
    reset_sha: str,
) -> None:
    """Drop what a landed reset just took the branch off, in memory.

    Both records name a commit only the reflog still has once the branch is
    back on the pre-rebase anchor: the approval says a push is owed for it,
    and a transfer's permission says what that push may carry a human's
    verdict over. Neither has anything left to be paid by, and both are staged
    rather than written so the park's own write is what makes the drop
    durable.
    """
    # Lazily bound for the reason the comment and guard owners are: the size
    # gate sits in the workflow layer above this package, and binding it at
    # module load would make every git-side import pay for the stage tree.
    from orchestrator.workflow.stages.implementing import (
        late_parks,
        late_records,
        late_transfer,
    )
    if late_parks._approved_commit(context.state) != reset_sha:
        late_parks._forget_approval(context.state)
    late_transfer._abandoned_authorization(
        late_records._gate(
            context.gh, context.spec, context.issue, context.state,
            context.worktree,
        ),
        reset_sha,
    )


def _forgets_the_unpayable_handoff(
    context: _AutoRebaseContext | _AutoRebaseRecoveryContext,
) -> None:
    """Drop what an interrupted attempt owed a pull request that is over.

    The anchor is not the whole of what one attempt leaves. Between the gate
    and the push it records a DEBT -- one commit still owed a publication, and
    the head that publication is pinned to -- and, on an issue whose exemption
    the rewrite is about to move, a PERMISSION for the write behind that push
    to carry the verdict over. Both are claims about a push onto one pull
    request, and a merged or closed one can never receive it.

    Left standing beside a cleared anchor, the debt is worse than stale. The
    reconciliation ahead of every handler reads it as a commit the pull
    request has not received, tries to publish it, and cannot even enter a
    publication that is over -- so it parks the issue on a reading nobody can
    take, and the stage that would have finalized a merged pull request to
    `done` never runs. So the record goes with the attempt that made it,
    in the attempt's own write.

    The debt is dropped whatever it is leased to, because while an anchor is
    pinned there is nothing else it could belong to: no stage handler runs
    over an outstanding attempt, so the approval standing beside one is that
    attempt's own. The permission is held to its own rule instead -- only an
    `authorized` record this reader can vouch for, and only one made over the
    head the anchor names -- because a `published` one describes a transfer
    that already happened and an exemption that has already moved, which the
    merge carried with it.
    """
    # Lazily bound for the reason the comment and guard owners are: the debt,
    # the permission, and the subject they are read on sit in the workflow
    # layer above this package.
    from orchestrator.workflow.stages.implementing import (
        late_parks,
        late_records,
        late_transfer,
    )
    late_parks._forget_approval(context.state)
    late_transfer._abandoned_authorization(
        late_records._gate(
            context.gh, context.spec, context.issue, context.state,
            context.worktree,
        ),
        str(context.pending_pre_rebase_sha or ""),
    )


def _prepare_recovered_rebase_state(
    context: _AutoRebaseRecoveryContext,
) -> None:
    """Clear the recovery anchor and commit any pending human retry.

    The three things every finish on this route owes its own write, staged
    together so no road can make one of them and forget another. The retry is
    the one that is easy to lose: a parked attempt is re-entered only because
    a human replied, and a finish that dropped the anchor without spending
    that reply would leave the issue routed to a stage that stands down on the
    auto-rebase park still flagged beside it, with nothing left to bring the
    tick back.
    """
    if context.unparking_consumed_max is not None:
        context.state.set(
            "last_action_comment_id", context.unparking_consumed_max,
        )
        context.state.set(_AWAITING_HUMAN, False)
        context.state.set(_PARK_REASON, None)
    _clears_the_attempt(context.state)
    context.state.set(_REVIEW_ROUND, 0)


def _announced(context, published: str) -> None:
    """Record that this finish has said what it did, before it routes.

    The one write in a finish whose whole purpose is the window behind it.
    Everything a finish announces -- the notice on the pull request, the audit
    event on both sinks -- goes out before the relabel, and the write that
    clears the record of the attempt goes out after it. A process lost in
    between comes back to an attempt that looks unfinished, and announcing it
    again puts a second `base_rebased` on the stream and a second notice on
    the pull request for one publication that happened once.

    Written while the anchor is still pinned, and that is deliberate: the
    anchor is what brings the recovery back at all, so this write may say only
    that the announcement was made and must leave every other field of the
    attempt exactly where it is. The clear rides the finish's own last
    write, which is what keeps the anchor standing until every road is
    behind it.
    """
    context.state.set(_PENDING_ANNOUNCED_SHA, published)
    context.gh.write_pinned_state(context.issue, context.state)


def _post_recovered_rebase_notice(
    context: _AutoRebaseRecoveryContext, notice: str,
) -> None:
    """Post the recovery notice without blocking state finalization."""
    # Lazy import: the comment owner sits in the workflow layer above this
    # package, so binding it at module load would make every git-side
    # import pay for the GitHub client and prompt state it pulls in.
    from orchestrator.workflow.engine import comments as _comments
    try:
        _comments._post_pr_comment(
            context.gh, context.pr_number, context.state, notice,
        )
    except Exception:  # noqa: BLE001 - the PR notice is best effort at the GitHub boundary
        log.exception(
            "issue=#%s could not post auto-rebase recovery notice to "
            "PR #%s", context.issue.number, context.pr_number,
        )


def _emit_recovered_rebase_event(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
    method: str,
) -> None:
    """Emit the stable audit shape for a recovered auto-rebase."""
    context.gh.emit_event(
        "base_rebased",
        issue_number=context.issue.number,
        stage=stage_name(context.label),
        pr_number=context.pr_number,
        sha=local_head,
        method=method,
        review_round=0,
        retry_count=context.state.get("retry_count"),
    )


def _route_recovered_rebase(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
    method: str,
) -> bool:
    """Persist recovery progress and route only a current head to validation.

    The relabel goes ahead of the pinned write for the reason the publisher's
    own tail does, and leaves the same window: an issue already on
    `validating` whose comment still names the stage the attempt started
    from. The recovery reads that as this route's own last step rather than a
    publication somebody else moved the issue to.
    """
    if context.behind == 0:
        log.info(
            "issue=#%d auto-rebase recovery (%s): recovered head %s is "
            "current; routing %r -> validating",
            context.issue.number,
            method,
            local_head[:8],
            context.label,
        )
        context.gh.set_workflow_label(context.issue, WorkflowLabel.VALIDATING)
        context.gh.write_pinned_state(context.issue, context.state)
        return True
    context.gh.write_pinned_state(context.issue, context.state)
    log.info(
        "issue=#%d auto-rebase recovery (%s): recovered head %s is still "
        "%d commit(s) behind %s/%s; falling through to the normal rebase "
        "+ push flow",
        context.issue.number,
        method,
        local_head[:8],
        context.behind,
        context.spec.remote_name,
        context.spec.base_branch,
    )
    return False


def _finalize_recovered_rebase(
    context: _AutoRebaseRecoveryContext,
    *,
    local_head: str,
    method: str,
    notice: str,
) -> bool:
    """Finalize a recovered push and route it according to current base lag.

    The announcement comes first and is made durable before anything is
    cleared, so the window between it and the relabel is one a later tick can
    tell from an attempt that never got this far. What the clear rides is
    still the last write, since the anchor is what brings that tick back.
    """
    _post_recovered_rebase_notice(context, notice)
    _emit_recovered_rebase_event(context, local_head, method)
    _announced(context, local_head)
    _prepare_recovered_rebase_state(context)
    return _route_recovered_rebase(context, local_head, method)


def _write_the_finished_route(
    context: _AutoRebaseRecoveryContext,
) -> bool:
    """Make durable the finish an earlier tick made everywhere but the comment.

    The one terminal that announces nothing. Every other road here ends by
    posting a notice, filing an audit event, and routing the reviewer, and
    those are exactly what the interrupted tick had already done: they go out
    before the pinned write, so a process lost between the relabel and that
    write leaves them all behind it. Repeating them would put a second
    `base_rebased` on the stream -- under the stage the relabel moved to
    rather than the one the rebase ran from -- and a second notice on a pull
    request that was published once.

    What is left is the route and the write. The relabel is made where the
    issue is not already on it, because the announcement and the relabel are
    two steps and a tick lost between them left the second undone -- clearing
    the attempt without it would strand the issue on the stage the rebase ran
    from with no anchor left to correct it, and the reviewer would never be
    sent to the rewritten head. Made only where it is needed, since the write
    that puts a label back where it already is is a transition the graph does
    not describe and a second `stage_enter` on the stream.

    Then the write itself, and it is the same one every other finish here
    makes: the record of the attempt, which is the only thing bringing this
    recovery back, the round the reviewer is being asked to spend afresh on
    the rewritten head, and the park a human's reply released. That last one
    is why this road may not spell its own two fields. An announced attempt
    can be parked -- the push failed, a record could not be read -- and a
    human's reply is what re-enters this route; taking the finish without
    spending it leaves the issue on `validating` still flagged
    `awaiting_human` under an auto-rebase reason, which the stage below reads
    as a park of its own and stands down for, with no anchor left to bring
    anything back.

    All of it is held to the base lag the way every other finish here is. A
    base that advanced again while the process was down leaves the published
    head behind it, and routing a reviewer at a head that is already stale is
    what the lag check exists to stop: the record is made durable, the label
    is left where it is, and the tick falls through to the rebase that brings
    the branch forward -- whose own finish makes the route.
    """
    log.info(
        "issue=#%d auto-rebase recovery: an earlier tick announced this route "
        "and died before its own write; finishing it rather than saying any "
        "of it again",
        context.issue.number,
    )
    _prepare_recovered_rebase_state(context)
    if context.behind:
        return _falls_through_to_a_fresh_rebase(context)
    if context.label != WorkflowLabel.VALIDATING:
        context.gh.set_workflow_label(context.issue, WorkflowLabel.VALIDATING)
    context.gh.write_pinned_state(context.issue, context.state)
    return True


def _falls_through_to_a_fresh_rebase(
    context: _AutoRebaseRecoveryContext,
) -> bool:
    """Make this finish durable and leave the branch to the rebase it owes.

    The base moved again while the process was down, so the head the
    interrupted tick published is behind it already. Relabelling there would
    send the reviewer to a commit this same tick is about to replace, and
    clearing the record without a write would lose the finish altogether -- so
    the record goes down and the route does not, and the rebase behind this
    call makes its own.
    """
    context.gh.write_pinned_state(context.issue, context.state)
    log.info(
        "issue=#%d auto-rebase recovery: the finished route is recorded and "
        "the branch is still %d commit(s) behind %s/%s; falling through to "
        "the normal rebase + push flow",
        context.issue.number,
        context.behind,
        context.spec.remote_name,
        context.spec.base_branch,
    )
    return False
