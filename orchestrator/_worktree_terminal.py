# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Worktree terminal."""
from __future__ import annotations

from orchestrator import _worktree_lifecycle_state as _state
from orchestrator import worktree_lifecycle as _owner

GitHubClient = _owner.GitHubClient
config = _owner.config
log = _state.log


def _cleanup_question_worktree(
    spec: config.RepoSpec, issue_number: int, *, branch: str | None = None,
) -> None:
    """Tear down the per-issue worktree and local branch after a
    `_handle_question` tick.

    The question stage spawns the agent in the same `issue-N`
    worktree the implementing stage uses, but the agent is read-only
    -- it never commits or pushes. Leaving the worktree on disk
    between ticks lets the per-tick `_refresh_base_and_worktrees`
    treat it as a pre-PR worktree behind base and merge
    `origin/<base>` into it, accreting local commits on a read-only
    question branch. A later relabel to `implementing` then either
    trips the `question_unsafe_relabel` guard (worktree still on
    disk) or, if a stale local branch survives a worktree GC, falls
    through to the recovered-worktree push path. Either way the
    "question responses without PRs / read-only" contract breaks.

    Called from every safe-exit of `_handle_question` (answer,
    silent, no-resume return). Skipped for the parks that
    explicitly KEEP the worktree so the operator can inspect what
    the misbehaving agent did (`question_commits`, `question_dirty`,
    `question_timeout`); the workflow-label skip in
    `_sync_worktree_with_base` then prevents base sync from
    mutating those kept worktrees behind the operator's back.

    Removes the worktree AND the local branch. The next answer /
    resume / relabel rebuilds the worktree from a fresh
    `origin/<base>`; agent session state lives in pinned state, not
    in the worktree, so resume across the cleanup works.

    No remote-side step -- the question stage never pushed, so
    there is no remote branch to delete. Best-effort: each step
    swallows its own error so cleanup never raises out of the
    handler. Serialized via `_target_root_lock` for the same
    `.git/config.lock` reason described on `_ensure_worktree`.
    """
    if branch is None:
        branch = _owner._branch_name(spec, issue_number)
    _owner._remove_issue_worktree(spec, issue_number, log_prefix="question ")
    _owner._delete_local_issue_branch(
        spec, issue_number, branch, log_prefix="question ",
    )


def _cleanup_terminal_branch(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue_number: int,
    *,
    branch: str | None = None,
) -> None:
    """Remove the per-issue worktree and delete the local + remote branches.

    Called after the PR for `issue_number` reached a terminal state -- either
    merged externally by a human (the orchestrator is permanently manual-
    merge-only and never calls `gh.merge_pr`) or closed without merge.
    Best-effort: each step swallows its own error so a leftover
    worktree or branch never raises out of the terminal handler -- by the
    time we reach here the issue has already flipped to `done` or
    `rejected`, and a stale ref is tidiness, not correctness.

    `branch` overrides the default `_branch_name(spec, issue_number)`
    so terminal cleanup of an in-flight issue that was opened on the
    legacy `orchestrator/issue-<n>` ref reaps that branch (not the new
    namespaced one that was never pushed).

    The branch name is constrained to the orchestrator-owned
    `orchestrator/...` namespace (verified via the
    `orchestrator/`-prefix check in `_resolve_branch_name` upstream),
    so this cleanup cannot touch an arbitrary branch.

    Order matters: the worktree must come down before `git branch -D`,
    because git refuses to delete a branch that's still checked out in a
    worktree. Remote delete is last so a local-side failure does not block
    cleaning up the GitHub side (which is what the operator actually sees
    in the repo's branch list). All local `_git` calls run from
    `spec.target_root` so the multi-repo loop tidies the right clone.

    Both local-side steps are serialized by the per-target_root lock
    because `worktree remove` and `branch -D` write to the parent
    `.git/config` and `.git/refs`; without the lock a concurrent
    `_ensure_worktree` on another worker thread races on
    `.git/config.lock`. The remote delete is a GitHub-side HTTP call
    (no local git plumbing) and stays outside the lock.
    """
    if branch is None:
        branch = _owner._branch_name(spec, issue_number)

    # Each helper contains its own exception boundary so a local failure
    # cannot skip the next cleanup surface.
    _owner._remove_issue_worktree(spec, issue_number)
    _owner._delete_local_issue_branch(spec, issue_number, branch)

    try:
        gh.delete_remote_branch(branch)
    except Exception:
        log.exception(
            "issue=#%d remote branch %r delete raised", issue_number, branch,
        )
