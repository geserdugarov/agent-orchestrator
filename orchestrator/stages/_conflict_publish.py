# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Conflict publish."""
from __future__ import annotations

from orchestrator.stages import _conflict_state as _state
from orchestrator.stages import conflicts as _owner

_ConflictContext = _owner._ConflictContext
Path = _owner.Path
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
_CONFLICT_ROUND = _state._CONFLICT_ROUND
_REVIEW_ROUND = _state._REVIEW_ROUND


def _publish_clean_rebase(
    ctx: _ConflictContext,
    wt: Path,
    before_sha: str,
    conflict_round: int,
    pr_number,
) -> None:
    """Dispose of a clean `git rebase <remote>/<base>` outcome.

    Parks on a dirty tree; flips to `validating` without a push when the
    base had not moved (no-op rebase, still counted against the cap); or
    force-pushes the rebased head and flips to `validating`. The caller
    returns immediately after; every exit writes pinned state.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    # Dirty check before EITHER clean-rebase exit (no-op flip OR rebased-head
    # push): a pre-existing uncommitted edit (left by a previous tick that
    # crashed before its own dirty check ran) would otherwise survive a no-op
    # flip into validating, where the reviewer agent reads the worktree
    # directly. The reviewer would then vote on a tree that does NOT match the
    # PR head; the in_review HITL ready-ping would later advertise the PR as
    # ready for human merge with the reviewer's approval sitting against an
    # incorrect SHA, inviting a human merge over unreviewed content. Park
    # rather than push or flip, mirroring `_on_dirty_worktree`'s "refuse to
    # publish an incomplete branch" rule.
    dirty = _wf._worktree_dirty_files(wt)
    if dirty:
        _owner._park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} worktree has {len(dirty)} "
            f"uncommitted change(s) after `git rebase "
            f"{spec.remote_name}/{spec.base_branch}`; refusing to "
            "push or hand back to validating with a dirty tree.",
            reason="dirty_worktree",
        )
        return
    after_sha = _wf._head_sha(wt)
    if not after_sha or after_sha == before_sha:
        _owner._flip_base_up_to_date(ctx, conflict_round, pr_number, after_sha)
        return
    if not _wf._push_branch(
        spec, wt, _wf._resolve_branch_name(ctx.state, spec, ctx.issue.number),
        force_with_lease=before_sha or None,
    ):
        _owner._park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} git push failed after auto-rebasing "
            f"`{spec.remote_name}/{spec.base_branch}`; "
            "see orchestrator logs.",
            reason="push_failed",
        )
        return
    # Pushed branch diff -> hand straight back to validating; the single docs
    # pass runs after final reviewer approval.
    _owner._hand_resolved_round_to_validating(
        ctx, conflict_round, pr_number,
        outcome="base_rebased_clean", sha=after_sha,
    )


def _flip_base_up_to_date(
    ctx: _ConflictContext, conflict_round: int, pr_number, after_sha,
) -> None:
    """Hand a no-op base rebase (branch already current) back to `validating`.

    Increments `conflict_round` even though no diff was applied: an unmergeable
    PR blocked purely by branch protection / required reviewers (PyGithub
    cannot tell those from a content conflict) would otherwise loop
    in_review <-> resolving_conflict forever with the cap never firing.
    Counting the no-op against the cap surfaces it within MAX_CONFLICT_ROUNDS
    ticks. Does NOT stamp `last_conflict_resolved_at` -- nothing was resolved.
    """
    from orchestrator import workflow as _wf

    _wf.log.info(
        "issue=#%d resolving_conflict: branch already up-to-date with %s/%s",
        ctx.issue.number, ctx.spec.remote_name, ctx.spec.base_branch,
    )
    ctx.state.set(_REVIEW_ROUND, 0)
    ctx.state.set(_CONFLICT_ROUND, conflict_round + 1)
    _owner._emit_conflict_round_incremented(
        ctx,
        pr_number=int(pr_number),
        new_round=conflict_round + 1,
        outcome="base_up_to_date",
        sha=after_sha,
    )
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _resolve_conflicts_with_agent(
    ctx: _ConflictContext,
    conflicted_files,
    before_sha: str,
    conflict_round: int,
) -> None:
    """Resume the dev session to resolve real rebase content conflicts.

    Builds the conflict-resolution prompt from the conflicted files,
    resumes the locked backend, and funnels the result through
    `_post_conflict_resolution_result` (leasing the push against
    `before_sha`). Returns without touching durable state when a live
    pause lands mid-run.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    fix_prompt = _wf._build_conflict_resolution_prompt(
        f"{spec.remote_name}/{spec.base_branch}", conflicted_files,
    )
    run = _owner._run_conflict_resume(ctx, fix_prompt)
    # Live pause applied mid-run: return before
    # `_post_conflict_resolution_result` pushes / relabels / writes pinned
    # state -- the resolved commit stays on the branch until the label is
    # removed.
    if run.paused:
        return
    _owner._post_conflict_resolution_result(
        ctx, run, before_sha, conflict_round,
        force_with_lease=before_sha or None,
    )
