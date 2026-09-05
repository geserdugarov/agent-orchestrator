# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The refusals that stand between a finished rebase and its force-push.

Each one answers the same question with "not this branch, not this tick":
the rebased HEAD cannot be read, the rewrite moved nothing, the tree came
back with uncommitted edits, or the lease push was rejected. Three of them
end in the shared reset-and-park tail, because the pre-rebase SHA is the
head the remote PR still carries -- publishing past any of them would leave
the reviewer voting on a tree the PR does not have, and parking is what
stops the same tick's stage handlers from reading the rewritten HEAD. The
no-op exit is the one that is not a failure: nothing moved, so only the
crash-recovery anchor has to be dropped.
"""
from __future__ import annotations

from orchestrator import config
from orchestrator.git.base_sync import persistence
from orchestrator.git.base_sync.models import _AutoRebaseContext
from orchestrator.git.base_sync.state import (
    _REASON_AUTO_BASE_REBASE_FAILED,
    _REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    log,
)


def _park_unreadable_post_rebase_head(
    context: _AutoRebaseContext,
    before_sha: str,
) -> None:
    """Restore the known PR head when the rebased HEAD cannot be read."""
    log.error(
        "issue=#%d cannot read local HEAD after auto base rebase; "
        "resetting to pre-rebase SHA and parking awaiting human",
        context.issue.number,
    )
    spec = context.spec
    before_short = before_sha[:8]
    persistence._reset_clear_and_park(
        context,
        before_sha,
        message=(
            f"{config.HITL_MENTIONS} PR #{context.pr_number} is "
            f"{context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`. "
            "The auto rebase ran but the orchestrator could not "
            "read local `HEAD` afterwards. HEAD has been reset to "
            f"the pre-rebase SHA `{before_short}` so the worktree "
            "still matches the remote PR head. Inspect the "
            "worktree's git state and reply on this issue with "
            "anything to retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )


def _finish_noop_auto_rebase(context: _AutoRebaseContext) -> None:
    """Clear the recovery anchor when the rebase leaves HEAD unchanged."""
    log.info(
        "issue=#%d base rebase was a no-op despite %d commit(s) "
        "behind %s/%s; leaving label alone",
        context.issue.number,
        context.behind,
        context.spec.remote_name,
        context.spec.base_branch,
    )
    persistence._clears_the_attempt(context.state)
    context.gh.write_pinned_state(context.issue, context.state)


def _park_dirty_auto_rebase(
    context: _AutoRebaseContext,
    before_sha: str,
    dirty_files: list[str],
) -> None:
    """Reset and park rather than publish a rebase with worktree edits."""
    log.warning(
        "issue=#%d worktree has %d uncommitted change(s) after "
        "auto base rebase; resetting HEAD and parking awaiting human",
        context.issue.number,
        len(dirty_files),
    )
    spec = context.spec
    persistence._reset_clear_and_park(
        context,
        before_sha,
        message=(
            f"{config.HITL_MENTIONS} PR #{context.pr_number} is "
            f"{context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}` "
            "and the auto rebase landed cleanly but left "
            f"{len(dirty_files)} uncommitted change(s) on the worktree. "
            "Local HEAD has been reset to the pre-rebase SHA and "
            "untracked files cleaned (use `git reflog` if you need "
            "the discarded edits). Investigate the smudge filter / "
            "hook / external race that produced the dirty tree, "
            "then reply on this issue with anything to retry."
        ),
        reason="auto_base_rebase_dirty",
        clean=True,
    )


def _park_failed_auto_rebase_push(
    context: _AutoRebaseContext,
    before_sha: str,
    branch: str,
) -> None:
    """Reset and park after a force-with-lease rejection or push failure."""
    spec = context.spec
    before_short = (before_sha or "")[:8]
    persistence._reset_clear_and_park(
        context,
        before_sha,
        message=(
            f"{config.HITL_MENTIONS} PR #{context.pr_number} is "
            f"{context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`; "
            "the orchestrator rebased the worktree cleanly but pushing "
            "the rewritten branch (`--force-with-lease` against "
            f"`{before_short}`) failed. Local HEAD has "
            "been reset to the pre-rebase SHA so the worktree still "
            "matches the remote PR head. Most likely the PR branch "
            "was updated out-of-band; investigate the remote "
            f"`{branch}` and reply on this issue with anything once "
            "the branch is ready for the orchestrator to re-attempt "
            "the auto-rebase on the next polling tick."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    log.warning(
        "issue=#%d auto base rebase pushed nothing (lease rejection "
        "or push failure); local HEAD reset and issue parked awaiting "
        "human so the in_review / fixing / validating / documenting "
        "handlers do not process the issue on a behind-base PR head "
        "this tick",
        context.issue.number,
    )
