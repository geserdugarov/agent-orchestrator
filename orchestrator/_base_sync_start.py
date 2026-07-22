# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Base sync start."""
from __future__ import annotations

from orchestrator import _base_sync_state as _state
from orchestrator import base_sync as _owner

_AutoRebaseContext = _owner._AutoRebaseContext
Optional = _owner.Optional
PullRequest = _owner.PullRequest
config = _owner.config
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_PARK_REASON = _state._PARK_REASON
_PENDING_PUSH_SHA = _state._PENDING_PUSH_SHA
_REASON_AUTO_BASE_REBASE_FAILED = _state._REASON_AUTO_BASE_REBASE_FAILED
log = _state.log


def _park_unreadable_pre_rebase_head(context: _AutoRebaseContext) -> None:
    """Fail closed when the lease and recovery anchor cannot be read."""
    log.error(
        "issue=#%d cannot read local HEAD before auto base rebase; "
        "parking awaiting human (no rebase attempted)",
        context.issue.number,
    )
    spec = context.spec
    _owner._park_auto_rebase_failure(
        context.gh,
        context.issue,
        context.state,
        message=(
            f"{config.HITL_MENTIONS} PR #{context.pr_number} is "
            f"{context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`, "
            "but the orchestrator could not read local `HEAD` on "
            "the per-issue worktree before attempting the auto "
            "rebase. Force-with-lease pushes and the crash-recovery "
            "anchor both require a known pre-rebase SHA, so the "
            "rebase was skipped. Inspect the worktree's git state "
            "and reply on this issue with anything to retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )


def _record_auto_rebase_attempt(
    context: _AutoRebaseContext,
    before_sha: str,
    consumed_comment_id: Optional[int],
) -> None:
    """Persist the recovery anchor and any retry unpark before git runs."""
    if consumed_comment_id is not None:
        context.state.set("last_action_comment_id", consumed_comment_id)
        context.state.set(_AWAITING_HUMAN, False)
        context.state.set(_PARK_REASON, None)
    context.state.set(_PENDING_PUSH_SHA, before_sha)
    context.gh.write_pinned_state(context.issue, context.state)


def _handle_failed_auto_rebase(
    context: _AutoRebaseContext,
    pr: PullRequest,
    conflicted_files: list[str],
) -> None:
    """Abort a failed rebase, then route conflicts or park other failures."""
    abort = _owner._git_hardened("rebase", "--abort", cwd=context.worktree)
    if abort.returncode != 0:
        log.warning(
            "issue=#%d base rebase failed and abort failed: %s",
            context.issue.number,
            (abort.stderr or "").strip(),
        )
    context.state.set(_PENDING_PUSH_SHA, None)
    if conflicted_files:
        _owner._route_pr_worktree_to_resolving_conflict(
            context.gh,
            context.spec,
            context.issue,
            context.state,
            context.pr_number,
            label=context.label,
            behind=context.behind,
            conflicted_files=conflicted_files,
            pr_head_sha=getattr(pr.head, "sha", None) or None,
        )
        return

    log.warning(
        "issue=#%d base rebase failed without conflicted files; "
        "parking awaiting human (refresh-only recovery on a new "
        "issue comment)",
        context.issue.number,
    )
    spec = context.spec
    _owner._park_auto_rebase_failure(
        context.gh,
        context.issue,
        context.state,
        message=(
            f"{config.HITL_MENTIONS} PR #{context.pr_number} is "
            f"{context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}` "
            "and the auto rebase failed for a non-conflict reason "
            "(planted hook, smudge filter, permissions, ...). The "
            "worktree was restored to the pre-rebase SHA via "
            "`git rebase --abort`. Investigate the worktree / hooks, "
            "then reply on this issue with anything once the "
            "underlying problem is fixed; the next polling tick will "
            "re-attempt the auto rebase."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )


def _start_auto_rebase(
    context: _AutoRebaseContext,
    pr: PullRequest,
    consumed_comment_id: Optional[int],
) -> Optional[str]:
    """Anchor and execute the rebase, returning the known pre-rebase SHA."""
    before_sha = _owner._head_sha(context.worktree) or ""
    if not before_sha:
        _owner._park_unreadable_pre_rebase_head(context)
        return None
    _owner._record_auto_rebase_attempt(context, before_sha, consumed_comment_id)
    succeeded, conflicted_files = _owner._rebase_base_into_worktree(
        context.spec, context.worktree,
    )
    if not succeeded:
        _owner._handle_failed_auto_rebase(context, pr, conflicted_files)
        return None
    return before_sha
