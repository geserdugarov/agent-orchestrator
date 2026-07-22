# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Base sync recovery decisions."""
from __future__ import annotations

from orchestrator import _base_sync_state as _state
from orchestrator import base_sync as _owner

_AutoRebaseRecoveryContext = _owner._AutoRebaseRecoveryContext
_AutoRebaseRecoverySnapshot = _owner._AutoRebaseRecoverySnapshot
config = _owner.config
_REASON_AUTO_BASE_REBASE_PUSH_FAILED = _state._REASON_AUTO_BASE_REBASE_PUSH_FAILED
log = _state.log


def _already_published_recovery_notice(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
) -> str:
    """Format the notice for a recovery push that landed before restart."""
    short_head = local_head[:8]
    notice = (
        f":mag: Recovered an interrupted auto-rebase for PR "
        f"#{context.pr_number}; the new head `{short_head}` was "
        "already published before the orchestrator restart."
    )
    if context.behind == 0:
        return (
            notice
            + f" Routing `{context.label}` -> `validating` so the "
            "reviewer re-runs against the rewritten branch."
        )
    return (
        notice
        + f" Base advanced again by {context.behind} commit(s)"
        " since the interrupted rebase; rebasing once "
        "more before routing to `validating`."
    )


def _pushed_recovery_notice(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
) -> str:
    """Format the notice for a recovery push reissued this tick."""
    short_head = local_head[:8]
    notice = (
        f":mag: Recovered an interrupted auto-rebase for PR "
        f"#{context.pr_number}; pushed the recovered head "
        f"`{short_head}`."
    )
    if context.behind == 0:
        return f"{notice} Routing `{context.label}` -> `validating`."
    return (
        notice
        + f" Base advanced again by {context.behind} commit(s) "
        "since the interrupted rebase; rebasing once more "
        "before routing to `validating`."
    )


def _finalize_already_published_recovery(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Finalize state after confirming that the interrupted push landed."""
    return _owner._finalize_recovered_rebase(
        context,
        local_head=snapshot.local_head,
        method="crash_recovery_relabel_only",
        notice=_owner._already_published_recovery_notice(
            context, snapshot.local_head,
        ),
    )


def _reject_unknown_recovery_comparison(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Park when unequal heads cannot be classified as ahead or behind."""
    log.warning(
        "issue=#%d auto-rebase recovery: local HEAD (`%s`) differs "
        "from remote PR head (`%s`) but `_branch_ahead_behind` "
        "returned `(0, 0)`; aborting recovery and parking awaiting "
        "human",
        context.issue.number,
        snapshot.local_head[:8],
        snapshot.remote_head[:8],
    )
    local_short = snapshot.local_head[:8]
    remote_short = snapshot.remote_head[:8]
    return _owner._abort_recovery_unverified(
        context,
        f"local HEAD `{local_short}` differs from remote "
        f"PR head `{remote_short}` but "
        "`_branch_ahead_behind` returned `(0, 0)`, which means the "
        "remote-tracking ref we just fetched is unexpectedly missing "
        "-- the path the recovery would take next cannot be determined "
        "safely.",
    )


def _park_diverged_recovery(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor instead of overwriting an out-of-band PR update."""
    spec = context.spec
    local_short = snapshot.local_head[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    _owner._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: local worktree "
            f"(`{local_short}`) is {snapshot.ahead} ahead "
            f"and {snapshot.behind} behind remote "
            f"`{spec.remote_name}/{snapshot.branch}` -- the "
            "remote PR branch was updated out-of-band during the "
            "interrupted auto rebase. HEAD has been reset to the pre-"
            f"rebase SHA `{pre_rebase_short}`. "
            "Investigate the remote PR head and reply on this issue "
            "with anything once the divergence is reconciled."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    return True


def _park_dirty_recovery(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
    dirty_files: list[str],
) -> bool:
    """Reset and clean a recovered rebase that carries worktree changes."""
    local_short = snapshot.local_head[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    _owner._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: the rebased worktree (recovered "
            f"from a prior tick, HEAD `{local_short}`) "
            f"carries {len(dirty_files)} uncommitted change(s). HEAD "
            "has been reset to the pre-rebase SHA "
            f"`{pre_rebase_short}` and untracked "
            "files cleaned (use `git reflog` if you need the "
            "discarded edits). Investigate, then reply on this issue "
            "with anything to retry."
        ),
        reason="auto_base_rebase_dirty",
        clean=True,
    )
    return True


def _park_failed_recovery_push(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor after a recovered force-push fails."""
    local_short = snapshot.local_head[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    _owner._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: `--force-with-lease` push of the "
            f"recovered rebase (`{local_short}`, lease "
            f"against `{pre_rebase_short}`) failed. "
            "HEAD has been reset to the pre-rebase SHA. Most likely "
            "the remote PR branch was updated out-of-band; investigate "
            "and reply on this issue with anything to retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    return True
