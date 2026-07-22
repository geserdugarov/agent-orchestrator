# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Base sync recovery."""
from __future__ import annotations

import inspect
from typing import Any

from orchestrator import _base_sync_state as _state
from orchestrator import base_sync as _owner

_AutoRebaseRecoveryContext = _owner._AutoRebaseRecoveryContext
_AutoRebaseRecoverySnapshot = _owner._AutoRebaseRecoverySnapshot
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
PinnedState = _owner.PinnedState
config = _owner.config
_PR_REFRESH_DETOUR_LABELS = _state._PR_REFRESH_DETOUR_LABELS


_RECOVERY_SIGNATURE = inspect.Signature((
    inspect.Parameter("gh", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("spec", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("issue", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("state", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("worktree", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("pr_number", inspect.Parameter.KEYWORD_ONLY),
    inspect.Parameter("label", inspect.Parameter.KEYWORD_ONLY),
    inspect.Parameter(
        "pending_pre_rebase_sha",
        inspect.Parameter.KEYWORD_ONLY,
    ),
    inspect.Parameter("behind", inspect.Parameter.KEYWORD_ONLY, default=0),
    inspect.Parameter(
        "unparking_consumed_max",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
    ),
))


def _retry_recovery_push(
    context: _AutoRebaseRecoveryContext,
    snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Publish a verified ahead-only recovery head and finalize its state."""
    dirty_files = _owner._worktree_dirty_files(context.worktree)
    if dirty_files:
        return _owner._park_dirty_recovery(context, snapshot, dirty_files)
    if not _owner._push_branch(
        context.spec,
        context.worktree,
        snapshot.branch,
        force_with_lease=context.pending_pre_rebase_sha,
    ):
        return _owner._park_failed_recovery_push(context, snapshot)
    return _owner._finalize_recovered_rebase(
        context,
        local_head=snapshot.local_head,
        method="crash_recovery_pushed",
        notice=_owner._pushed_recovery_notice(context, snapshot.local_head),
    )


def _recover_pending_auto_base_rebase_context(
    context: _AutoRebaseRecoveryContext,
) -> bool:
    """Route an interrupted auto-rebase from verified local/remote state."""
    if context.label not in _PR_REFRESH_DETOUR_LABELS:
        return _owner._clear_ineligible_recovery(context)

    snapshot = _owner._fetch_recovery_snapshot(context)
    if snapshot is None:
        return True
    if (
        snapshot.local_head
        and snapshot.local_head == context.pending_pre_rebase_sha
    ):
        return _owner._clear_unchanged_recovery(context)

    return _owner._route_recovery_snapshot(context, snapshot)


def _route_recovery_snapshot(
    context: _AutoRebaseRecoveryContext, snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Route a changed-head recovery from its completed local/remote compare."""
    snapshot = _owner._complete_recovery_snapshot(context, snapshot)
    if snapshot is None:
        return True
    if snapshot.local_head and snapshot.local_head == snapshot.remote_head:
        return _owner._finalize_already_published_recovery(context, snapshot)
    if snapshot.ahead == 0 and snapshot.behind == 0:
        return _owner._reject_unknown_recovery_comparison(context, snapshot)
    if snapshot.behind > 0:
        return _owner._park_diverged_recovery(context, snapshot)
    return _owner._retry_recovery_push(context, snapshot)


def _recover_pending_auto_base_rebase(
    *args: Any,
    **kwargs: Any,
) -> bool:
    """Finalize a clean auto-base-rebase interrupted by a prior crash.

    The pinned pre-rebase SHA distinguishes an unchanged worktree, an
    already-published rewrite, an ahead-only rewrite that still needs a
    push, and a branch that diverged through an out-of-band update. Returns
    False only when HEAD still equals the anchor and the normal rebase flow
    should continue on the same tick.
    """
    bound_fields = _RECOVERY_SIGNATURE.bind(*args, **kwargs)
    bound_fields.apply_defaults()
    context = _AutoRebaseRecoveryContext(**bound_fields.arguments)
    return _owner._recover_pending_auto_base_rebase_context(context)


_recover_pending_auto_base_rebase.__signature__ = _RECOVERY_SIGNATURE
