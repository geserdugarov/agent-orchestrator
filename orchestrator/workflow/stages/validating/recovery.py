# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The parks that can clear without anyone commenting.

A push that lost a race and an agent killed by its own timeout are both
conditions the next tick can simply re-attempt, but neither produces the human
reply the awaiting-human resume waits for. Without a silent retry the issue
sits parked forever on a failure that already went away.

So the recovery runs quietly and answers in one of three words. It must not
spawn an agent or post anything -- the caller owns the visible side, so a tick
that is still stuck produces no churn at all. It IS allowed to move the review
round, and it is the only writer permitted to while the park flags are still
set: a timeout that had already committed gets its push finished here, and
that landed commit is a head the reviewer has not seen.

`push_failed` and `agent_timeout` are the two that actually touch git;
the reviewer-side reasons clear on sight, because there is no dev work to
finish, only a reviewer to re-spawn. Every probe fails closed to `"stuck"` --
a missing worktree, a dirty tree, an unreadable `pre_dev_fix_sha`, a push that
fails again -- since leaving the park standing costs a poll and publishing
blind costs the PR.
"""
from __future__ import annotations

from github.Issue import Issue

from orchestrator import config
from orchestrator.git import authentication as _authentication
from orchestrator.git.verification import probes as _verification_probes
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.stages.validating import dev_fix as _dev_fix
from orchestrator.workflow.stages.validating import state as _state


def _recover_failed_push(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> str:
    worktree = _worktree_paths._worktree_path(spec, issue.number)
    if not worktree.exists():
        return _state._OUTCOME_STUCK
    branch = _worktree_paths._resolve_branch_name(state, spec, issue.number)
    if not _authentication._push_branch(spec, worktree, branch):
        return _state._OUTCOME_STUCK
    _dev_fix._bump_review_round(state)
    return _state._OUTCOME_PUSHED


def _recover_timed_out_fix(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> str:
    worktree = _worktree_paths._worktree_path(spec, issue.number)
    if (
        not worktree.exists()
        or _verification_probes._worktree_dirty_files(worktree)
    ):
        return _state._OUTCOME_STUCK
    before_sha = state.get(_state._PRE_DEV_FIX_SHA)
    if not isinstance(before_sha, str):
        return _state._OUTCOME_STUCK
    current_sha = _verification_probes._head_sha(worktree)
    if not current_sha or current_sha == before_sha:
        state.set(_state._PRE_DEV_FIX_SHA, None)
        return "cleared"
    branch = _worktree_paths._resolve_branch_name(state, spec, issue.number)
    if not _authentication._push_branch(spec, worktree, branch):
        return _state._OUTCOME_STUCK
    state.set(_state._PRE_DEV_FIX_SHA, None)
    _dev_fix._bump_review_round(state)
    return _state._OUTCOME_PUSHED


def _try_recover_validating_transient_park(
    spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> str:
    """Quietly attempt to clear a transient validating park.

    Returns one of:
      * ``"stuck"`` -- the underlying condition has not resolved; caller
        leaves the park flags in place and returns silently.
      * ``"cleared"`` -- the park can be cleared, but nothing new
        landed on the PR (reviewer-only crash, or a dev-timeout that
        had not actually produced a commit). Caller clears the flags
        and stays on `validating` so the reviewer reruns.
      * ``"pushed"`` -- a dev fix was finished off during recovery
        (a deferred push of `push_failed`, or the trailing push of an
        `agent_timeout` that had committed before being killed).
        Caller clears the flags, resets stale approval state, and
        stays on `validating` so the reviewer re-evaluates the new
        head.

    Must not spawn the agent or post issue/PR comments -- the caller owns
    the visible side of the recovery so a still-stuck tick produces no
    churn.

    The helper IS allowed to update review-round bookkeeping when a fix
    landed during recovery (e.g. an agent_timeout where the dev had
    actually committed before timing out, and we finish the push here).
    Callers should not mutate the round themselves; this is the only
    write path while the park flags are still set.
    """
    park_reason = state.get(_state._PARK_REASON)
    if park_reason == _state._REASON_PUSH_FAILED:
        return _recover_failed_push(spec, issue, state)
    if park_reason in (_state._REASON_REVIEWER_TIMEOUT, _state._REASON_REVIEWER_FAILED):
        return "cleared"
    if park_reason == _state._REASON_AGENT_TIMEOUT:
        return _recover_timed_out_fix(spec, issue, state)
    return _state._OUTCOME_STUCK
