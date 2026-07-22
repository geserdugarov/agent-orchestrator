# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Conflict routing."""
from __future__ import annotations

from orchestrator.stages import _conflict_state as _state
from orchestrator.stages import conflicts as _owner

_ConflictContext = _owner._ConflictContext
_WorktreeSync = _owner._WorktreeSync
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
config = _owner.config
_CONFLICT_ROUND = _state._CONFLICT_ROUND


def _handle_resolving_conflict(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue
) -> None:
    """Drive an unmergeable PR back to mergeable.

    Rebase the per-issue branch onto `origin/<base>`. On a clean rebase
    that actually moved HEAD, push and flip to `validating` so the
    reviewer re-runs against the rebased tree; if the base hasn't moved
    (branch already up-to-date) skip the push and flip straight to
    `validating` too. On real content conflicts, resume the dev session
    on the locked backend with a conflict-resolution prompt, push the
    resolved commit, and likewise flip to `validating`. Docs do not run
    here: the single docs pass runs after the reviewer's final
    `VERDICT: APPROVED` handoff to `documenting` in
    `_handle_validating`, so every pushed conflict-resolution path
    targets `validating` directly. Cap loops via `MAX_CONFLICT_ROUNDS`
    (parks awaiting human on exhaustion). On agent timeout / dirty
    tree / push failure, park awaiting human and let the operator
    unstick.

    Rebasing rewrites commit SHAs, so every pushed rebase resets
    `review_round`; validation must re-approve the rebased branch before
    any merge gate can pass.
    """
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    ctx = _ConflictContext(gh, spec, issue, state)
    pr_number = state.get("pr_number")

    if pr_number is None:
        _owner._park_conflict_missing_pr_number(ctx)
        return

    pr = gh.get_pr(int(pr_number))

    # Drain the shared PR/issue terminal arcs (merged PR -> `done`,
    # closed PR -> `rejected`, open PR + manually-closed issue ->
    # `rejected` without branch cleanup). The merged branch fires for
    # both "human merged after resolving conflicts manually" and
    # "Resolves #N auto-closed the issue when the PR merged"; the
    # open-PR + closed-issue arc only fires for issues a human closed
    # directly.
    #
    # Caveat carried over from the inline version: once the helper
    # flips a manually-closed (PR-still-open) issue to `rejected`, the
    # dispatcher's terminal-label branch is a no-op AND
    # `list_pollable_issues` only sweeps closed issues still labeled
    # `in_review` / `resolving_conflict`. A later PR close is never
    # observed by the orchestrator, so the operator must clean up the
    # worktree, local branch, and remote branch manually for the
    # "close issue first, then close PR" ordering.
    if _wf._drain_review_pr_terminals(
        gh, spec, issue, state, pr, stage="resolving_conflict",
    ):
        return

    # User-content drift: a human edited the issue body while the dev
    # was resolving conflicts. Resuming with the new body+comments lets
    # the dev decide whether the edit affects the conflict resolution.
    # On a successful pushed fix we hand straight to `validating` so the
    # reviewer re-runs against the updated tree; the docs pass is
    # deferred to the single post-approval hop. On an ack (no commit
    # but a reply) we stay in `resolving_conflict` without parking so a
    # harmless clarification doesn't stall the rebase.
    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        _owner._resume_on_user_content_change(ctx, pr_number, new_hash)
        return

    _owner._drive_conflict_rebase(ctx, pr, pr_number)


def _park_conflict_missing_pr_number(ctx: _ConflictContext) -> None:
    """Park a `resolving_conflict` issue that carries no pinned `pr_number`.

    Reaching here means a manual relabel from outside the normal route; the
    rebase / push paths all need the PR. An already-parked issue is left alone
    so the park comment is not re-posted every tick.
    """
    if ctx.state.get("awaiting_human"):
        return
    _owner._park_conflict(
        ctx,
        f"{config.HITL_MENTIONS} `resolving_conflict` without a pinned "
        "`pr_number`; manual relabeling suspected. Set the workflow "
        "label back to `validating` after fixing.",
        reason="missing_pr_number",
    )


def _drive_conflict_rebase(ctx: _ConflictContext, pr, pr_number) -> None:
    """Route past the awaiting-human resume and the conflict cap, then prepare
    the worktree and rebase.

    Resume-on-human-reply comes first: when parked awaiting human and a new
    comment arrived, resume the dev session on the in-progress rebase worktree
    with the human's text (mirrors `_handle_implementing`'s awaiting-human
    path so a `_on_question` / `_on_dirty_worktree` park can be unstuck by a
    comment, as the park messages invite). The cap parks awaiting human once
    `MAX_CONFLICT_ROUNDS` rounds have failed.
    """
    conflict_round = int(ctx.state.get(_CONFLICT_ROUND) or 0)

    if ctx.state.get("awaiting_human"):
        _owner._resume_awaiting_human(ctx, conflict_round)
        return

    if conflict_round >= config.MAX_CONFLICT_ROUNDS:
        _owner._park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} auto-conflict-resolution still failing "
            f"after {conflict_round} round(s) "
            f"(`MAX_CONFLICT_ROUNDS={config.MAX_CONFLICT_ROUNDS}`); manual "
            "intervention needed.",
            reason="conflict_cap",
        )
        return

    wt = _owner._prepare_conflict_worktree(ctx, pr, pr_number, conflict_round)
    if wt is None:
        return

    _owner._rebase_and_dispose(ctx, pr_number, conflict_round, wt)


def _prepare_conflict_worktree(
    ctx: _ConflictContext, pr, pr_number, conflict_round: int,
) -> Optional[Path]:
    """Restore the worktree, refresh remote refs, and reconcile a diverged or
    crash-recovered branch before the base rebase.

    Returns the worktree to rebase, or ``None`` when the tick is fully handled
    (a fetch failure / diverged-branch / dirty park, or a crash-recovery push
    that flipped straight to `validating`) and the caller must return.
    """
    from orchestrator import workflow as _wf

    wt = _owner._ensure_conflict_worktree(ctx)
    branch = _wf._resolve_branch_name(ctx.state, ctx.spec, ctx.issue.number)

    # Refresh `<remote>/<branch>` (the PR branch's remote tip) via the same
    # hardened authenticated path `_push_branch` uses. A stale local ref would
    # mis-classify a real "remote moved out from under us" as in-sync.
    if not _owner._fetch_pr_branch(ctx, wt, branch):
        return None

    # Check the worktree against the freshly-fetched remote PR head. Three
    # shapes: in sync `(0, 0)` proceeds to the base rebase; HEAD ahead
    # `(>0, 0)` is the crash-recovery case (a prior tick committed a
    # resolution but crashed before the push / post-push state write landed);
    # anything `behind > 0` is a stale or diverged worktree we refuse to
    # force-push over.
    sync = _WorktreeSync(
        wt, branch, *_wf._branch_ahead_behind(ctx.spec, wt, branch),
    )
    guard = _owner._guard_diverged_worktree(ctx, pr, sync)
    if guard.parked:
        return None
    if sync.ahead > 0 and _owner._push_recovered_commits(
        ctx, sync, conflict_round, pr_number, guard.publish_lease,
    ):
        return None

    # In sync (or fell through after a recovered push to reconcile a stale
    # base). Refresh `<remote>/<base>` so the upcoming rebase sees the current
    # base tip.
    if not _owner._fetch_base_ref(ctx, wt):
        return None
    return wt
