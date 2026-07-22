# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Base sync pr."""
from __future__ import annotations

import inspect
from typing import Any

from orchestrator import _base_sync_state as _state
from orchestrator import base_sync as _owner

_AutoRebaseContext = _owner._AutoRebaseContext
_AutoRebaseRequest = _owner._AutoRebaseRequest
_ConflictRouteContext = _owner._ConflictRouteContext
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
PinnedState = _owner.PinnedState
PullRequest = _owner.PullRequest
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
_CONFLICT_ROUND = _state._CONFLICT_ROUND
_PENDING_PUSH_SHA = _state._PENDING_PUSH_SHA
_REVIEW_ROUND = _state._REVIEW_ROUND
log = _state.log


_SYNC_PR_SIGNATURE = inspect.Signature((
    inspect.Parameter("gh", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("spec", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("issue", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("state", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("worktree", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("pr_number", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("behind", inspect.Parameter.POSITIONAL_OR_KEYWORD),
))
_CONFLICT_ROUTE_SIGNATURE = inspect.Signature((
    inspect.Parameter("gh", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("spec", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("issue", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("state", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("pr_number", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("label", inspect.Parameter.KEYWORD_ONLY),
    inspect.Parameter("behind", inspect.Parameter.KEYWORD_ONLY),
    inspect.Parameter("conflicted_files", inspect.Parameter.KEYWORD_ONLY),
    inspect.Parameter("pr_head_sha", inspect.Parameter.KEYWORD_ONLY),
))


def _sync_pr_worktree_to_base(*args: Any, **kwargs: Any) -> None:
    """Bring a behind-base PR-having issue back to merge-ready.

    On a clean rebase: rebase the worktree onto `origin/<base>`, push
    with `--force-with-lease` pinned to the pre-rebase SHA (so a
    concurrent foreign update on the remote PR branch rejects the
    push instead of being clobbered), reset `review_round` to 0, post
    an informational PR notice, and relabel to `validating` so the
    reviewer re-runs against the rewritten head. Docs do not run on
    this exit -- the single docs pass runs after the next reviewer
    approval via the final-docs handoff to `documenting` in
    `_handle_validating`. This is the only safe pattern for PR-having
    worktrees, since a local-only rebase without a push would diverge
    local HEAD from `pr.head.sha` and break every downstream gate
    that compares the two.

    Only when the rebase actually leaves conflicted files do we
    relabel to `resolving_conflict`: the handler then drives the dev
    agent to resolve the conflict, pushes, and bounces back to
    `validating`. This reserves the `resolving_conflict` label for
    real rebase conflicts (or an operator manual application) and
    keeps the merely-behind-base case off it -- the label no longer
    flips on a clean sibling-PR merge that the orchestrator can
    auto-rebase. `_handle_in_review` is also permanently manual-
    merge-only and just parks awaiting human attention on an
    unmergeable PR.

    Skipped (label stays put, no PR notice, no push) when:

    * The label is not one the refresh drives (only `validating` /
      `documenting` / `in_review` / `fixing`); `resolving_conflict`
      itself is also skipped because the handler runs this tick anyway
      and will do the rebase regardless.

    * `awaiting_human=True`. The orchestrator already parked the issue
      and an attempted auto-rebase here would either re-open work that
      the human is meant to resolve or undermine the
      `MAX_REVIEW_ROUNDS` / `MAX_CONFLICT_ROUNDS` caps that exist
      precisely to require human intervention after repeated failures.

    * The PR is no longer open. A merged PR advances `origin/<base>`,
      so the still-validating / still-in_review / still-fixing
      worktree pointed at the now-stale branch is naturally behind
      base; without this gate the refresh would push, post an
      "auto-rebased" notice, and relabel to `validating` on a PR the
      next handler call would finalize to `done`. Same for closed-
      without-merge if base advanced concurrently (handler would
      finalize to `rejected`). Leave terminal PR state to the
      existing stage logic. A `gh.get_pr` failure is treated as
      "leave it alone" -- the handler can retry on the next tick from
      a stable label rather than racing a half-known PR state from
      refresh.

    The watermark bump in `_handle_in_review`'s analogous unmergeable
    detour is deliberately NOT replicated here. That bump is safe
    in_review-side because `_handle_in_review` has already scanned new
    comments before the relabel (anything past the watermark has been
    consumed by the fix-loop or filtered as orchestrator-authored).
    The refresh-time flow runs BEFORE any handler scans comments, so
    `latest_comment_id` may include unread human "do not merge" /
    fix-request comments; advancing the watermark here would silently
    mark them consumed and later validation / merge would skip them.
    The orchestrator's own PR notice we just posted is filtered out
    via `orchestrator_comment_ids` on the next `_handle_in_review`
    scan, so leaving the watermark alone does not cause the
    orchestrator to "see" its own message as fresh feedback. The
    `pending_fix_*` bookmarks recorded by an `in_review` -> `fixing`
    route are similarly left untouched: the next handler that resumes
    that route still finds them, and a stale bookmark on a now-
    `validating` issue is harmless (the reviewer pass clears it
    naturally when it next bounces to `fixing`).

    Dirty worktrees abort the push: a pre-existing uncommitted edit
    would otherwise be force-pushed alongside the rebase result, and
    the validating reviewer would then vote on a tree that does NOT
    match the PR head. Mirrors `_handle_resolving_conflict`'s refuse-
    to-publish-an-incomplete-branch rule. A push failure (the lease
    rejection most commonly surfaces a diverged or crash-recovery
    branch) leaves the label alone too; the next tick can retry once
    the underlying divergence is reconciled.
    """
    bound_fields = _SYNC_PR_SIGNATURE.bind(*args, **kwargs)
    request = _AutoRebaseRequest(
        *bound_fields.arguments.values(),
    )
    _owner._sync_pr_worktree_context(
        request.to_context(_PENDING_PUSH_SHA),
    )


def _sync_pr_worktree_context(context: _AutoRebaseContext) -> None:
    """Run one refresh-time PR synchronization from normalized inputs."""
    if not _owner._auto_rebase_label_is_eligible(context):
        return

    retry = _owner._auto_rebase_retry_decision(context)
    if not retry.should_continue:
        return
    pr = _owner._open_auto_rebase_pr(context)
    if pr is None:
        return

    _owner._publish_auto_rebase_from_pr(context, pr, retry.consumed_comment_id)


def _publish_auto_rebase_from_pr(
    context: _AutoRebaseContext, pr: PullRequest, consumed_comment_id: Optional[int],
) -> None:
    """Complete the recovery / rebase / publish phase for an opened PR."""
    recovery = _owner._auto_rebase_recovery_decision(context, consumed_comment_id)
    if not recovery.should_continue:
        return
    if not _owner._normal_auto_rebase_can_start(context):
        return

    before_sha = _owner._start_auto_rebase(
        context, pr, recovery.consumed_comment_id,
    )
    if before_sha is None:
        return

    _owner._publish_auto_rebase(context, before_sha)


def _route_pr_worktree_to_resolving_conflict(
    *args: Any,
    **kwargs: Any,
) -> None:
    """Relabel a PR-having issue to `resolving_conflict` for real conflicts.

    Called by `_sync_pr_worktree_to_base` when the auto-rebase left
    unresolved conflicted files. Seeds `conflict_round` only when
    absent (so a re-entry preserves the cap counter and a perpetually-
    stuck PR can't ping-pong indefinitely), posts a PR notice naming
    the conflicted files, emits the `conflict_round` "entered" audit
    event, and flips the workflow label so the existing
    `_handle_resolving_conflict` handler picks the work up on the
    same tick (the handler runs after the refresh in `tick()`).

    `pr_head_sha` is the remote PR head SHA at the time the rebase
    was attempted -- threaded in by the caller from the same
    `gh.get_pr(pr_number)` it uses for the PR-state gate -- so the
    emitted `conflict_round` `action="entered"` record carries the
    same `sha` field every other emit site populates
    (`docs/observability.md` documents it as part of the event shape).
    """
    bound_fields = _CONFLICT_ROUTE_SIGNATURE.bind(*args, **kwargs)
    context = _ConflictRouteContext(**bound_fields.arguments)
    _owner._route_pr_worktree_conflict_context(context)


def _route_pr_worktree_conflict_context(
    context: _ConflictRouteContext,
) -> None:
    """Persist and announce a normalized auto-rebase conflict route."""
    base_ref = "/".join((
        context.spec.remote_name,
        context.spec.base_branch,
    ))
    # Match `_handle_in_review`'s seeding: only initialize `conflict_round`
    # when absent, so a re-entry preserves the cap counter and a
    # perpetually-stuck PR can't ping-pong between handlers indefinitely.
    if context.state.get(_CONFLICT_ROUND) is None:
        context.state.set(_CONFLICT_ROUND, 0)

    try:
        _owner._post_pr_comment(
            context.gh, context.pr_number, context.state,
            f":mag: PR is {context.behind} commit(s) behind "
            f"`{base_ref}` and the auto "
            f"rebase left {len(context.conflicted_files)} conflicted file(s); "
            "orchestrator is attempting auto-resolution via the dev "
            "agent (label: `resolving_conflict`).",
        )
    except Exception:
        log.exception(
            "issue=#%s could not post auto-rebase notice to PR #%s",
            context.issue.number, context.pr_number,
        )

    log.info(
        "issue=#%d behind %s/%s by %d commit(s) with %d conflicted "
        "file(s); routing %r -> resolving_conflict so the handler "
        "drives the dev agent",
        context.issue.number,
        context.spec.remote_name,
        context.spec.base_branch,
        context.behind,
        len(context.conflicted_files),
        context.label,
    )
    context.gh.emit_event(
        _CONFLICT_ROUND,
        issue_number=context.issue.number,
        stage=context.label,
        pr_number=context.pr_number,
        sha=context.pr_head_sha or None,
        action="entered",
        conflict_round=int(context.state.get(_CONFLICT_ROUND) or 0),
        review_round=int(context.state.get(_REVIEW_ROUND) or 0),
        retry_count=context.state.get("retry_count"),
    )
    context.gh.set_workflow_label(
        context.issue,
        WorkflowLabel.RESOLVING_CONFLICT,
    )
    context.gh.write_pinned_state(context.issue, context.state)


_sync_pr_worktree_to_base.__signature__ = _SYNC_PR_SIGNATURE
_route_pr_worktree_to_resolving_conflict.__signature__ = (
    _CONFLICT_ROUTE_SIGNATURE
)
