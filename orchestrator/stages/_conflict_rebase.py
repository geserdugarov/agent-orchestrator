# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Conflict rebase."""
from __future__ import annotations

from orchestrator.stages import _conflict_state as _state
from orchestrator.stages import conflicts as _owner

_ConflictContext = _owner._ConflictContext
Path = _owner.Path
config = _owner.config
_REVIEW_ROUND = _state._REVIEW_ROUND


def _fetch_pr_branch(ctx: _ConflictContext, wt: Path, branch: str) -> bool:
    """Fetch `<remote>/<branch>` into the worktree. Returns False (after
    parking) on fetch failure, True otherwise."""
    from orchestrator import workflow as _wf

    spec = ctx.spec
    fetch_branch = _wf._authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=wt,
    )
    if fetch_branch.returncode == 0:
        return True
    _wf.log.error(
        "issue=#%d branch fetch failed in resolving_conflict: %s",
        ctx.issue.number, (fetch_branch.stderr or "").strip(),
    )
    _owner._park_conflict(
        ctx,
        f"{config.HITL_MENTIONS} `git fetch {spec.remote_name} {branch}` "
        "failed during conflict resolution; see orchestrator logs.",
        reason="fetch_failed",
    )
    return False


def _fetch_base_ref(ctx: _ConflictContext, wt: Path) -> bool:
    """Fetch `<remote>/<base>` into the worktree. Returns False (after
    parking) on fetch failure, True otherwise."""
    from orchestrator import workflow as _wf

    spec = ctx.spec
    fetch_base = _wf._authed_fetch(
        spec,
        f"+refs/heads/{spec.base_branch}:"
        f"refs/remotes/{spec.remote_name}/{spec.base_branch}",
        cwd=wt,
    )
    if fetch_base.returncode == 0:
        return True
    _wf.log.error(
        "issue=#%d base fetch failed in resolving_conflict: %s",
        ctx.issue.number, (fetch_base.stderr or "").strip(),
    )
    _owner._park_conflict(
        ctx,
        f"{config.HITL_MENTIONS} "
        f"`git fetch {spec.remote_name} {spec.base_branch}` "
        "failed during conflict resolution; see orchestrator logs.",
        reason="fetch_failed",
    )
    return False


def _rebase_and_dispose(
    ctx: _ConflictContext, pr_number, conflict_round: int, wt: Path,
) -> None:
    """Rebase the worktree onto base, emit `merge_attempt`, and dispose.

    A clean rebase routes to `_publish_clean_rebase`; a rebase that failed
    without listing conflicted files parks; real content conflicts hand to
    `_resolve_conflicts_with_agent`.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    before_sha = _wf._head_sha(wt)
    succeeded, conflicted_files = _wf._rebase_base_into_worktree(spec, wt)
    ctx.gh.emit_event(
        "merge_attempt",
        issue_number=ctx.issue.number,
        stage="resolving_conflict",
        pr_number=int(pr_number),
        sha=before_sha or None,
        method="base_rebase",
        result=_owner._merge_result(succeeded, conflicted_files),
        conflict_round=conflict_round,
        review_round=int(ctx.state.get(_REVIEW_ROUND) or 0),
        retry_count=ctx.state.get("retry_count"),
    )

    if succeeded:
        _owner._publish_clean_rebase(ctx, wt, before_sha, conflict_round, pr_number)
        return

    if not conflicted_files:
        _owner._park_conflict(
            ctx,
            f"{config.HITL_MENTIONS} "
            f"`git rebase {spec.remote_name}/{spec.base_branch}` "
            "failed without listing conflicted files; manual intervention "
            "needed.",
            reason="rebase_failed_no_files",
        )
        return

    _owner._resolve_conflicts_with_agent(
        ctx, conflicted_files, before_sha, conflict_round,
    )


def _merge_result(succeeded: bool, conflicted_files) -> str:
    """Map a base-rebase outcome to the `merge_attempt` event's `result`."""
    if succeeded:
        return "success"
    return "conflict" if conflicted_files else "failed"
