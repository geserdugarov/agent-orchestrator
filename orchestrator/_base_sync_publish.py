# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Base sync publish."""
from __future__ import annotations

from orchestrator import _base_sync_state as _state
from orchestrator import base_sync as _owner

_AutoRebaseContext = _owner._AutoRebaseContext
_PENDING_PUSH_SHA = _state._PENDING_PUSH_SHA
_REVIEW_ROUND = _state._REVIEW_ROUND
log = _state.log


def _post_auto_rebase_notice(
    context: _AutoRebaseContext,
    after_sha: str,
) -> None:
    """Post the best-effort PR notice for a published clean rebase."""
    spec = context.spec
    after_short = after_sha[:8]
    try:
        _owner._post_pr_comment(
            context.gh,
            context.pr_number,
            context.state,
            f":mag: PR was {context.behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`; "
            "orchestrator auto-rebased the branch and re-pushed it. "
            f"Routing `{context.label}` -> `validating` so the reviewer "
            f"re-runs against the new head (`{after_short}`).",
        )
    except Exception:
        log.exception(
            "issue=#%s could not post auto-rebase notice to PR #%s",
            context.issue.number,
            context.pr_number,
        )


def _emit_auto_rebase_event(
    context: _AutoRebaseContext,
    after_sha: str,
) -> None:
    """Emit the stable audit shape for a published clean rebase."""
    context.gh.emit_event(
        "base_rebased",
        issue_number=context.issue.number,
        stage=context.label,
        pr_number=context.pr_number,
        sha=after_sha,
        method="auto_clean_rebase",
        review_round=int(context.state.get(_REVIEW_ROUND) or 0),
        retry_count=context.state.get("retry_count"),
    )


def _finalize_auto_rebase(
    context: _AutoRebaseContext,
    branch: str,
    after_sha: str,
) -> None:
    """Publish the notice, audit event, validating route, and pinned state."""
    _owner._post_auto_rebase_notice(context, after_sha)
    context.state.set(_PENDING_PUSH_SHA, None)
    context.state.set(_REVIEW_ROUND, 0)
    log.info(
        "issue=#%d auto base rebase pushed %s/%s -> %s; routing %r -> "
        "validating",
        context.issue.number,
        context.spec.remote_name,
        branch,
        after_sha[:8],
        context.label,
    )
    _owner._emit_auto_rebase_event(context, after_sha)
    context.gh.set_workflow_label(context.issue, "validating")
    context.gh.write_pinned_state(context.issue, context.state)


def _publish_auto_rebase(
    context: _AutoRebaseContext,
    before_sha: str,
) -> None:
    """Validate and force-publish a successfully rebased PR worktree."""
    after_sha = _owner._head_sha(context.worktree)
    if not after_sha:
        _owner._park_unreadable_post_rebase_head(context, before_sha)
        return
    if after_sha == before_sha:
        _owner._finish_noop_auto_rebase(context)
        return

    dirty_files = _owner._worktree_dirty_files(context.worktree)
    if dirty_files:
        _owner._park_dirty_auto_rebase(context, before_sha, dirty_files)
        return

    branch = _owner._resolve_branch_name(
        context.state, context.spec, context.issue.number,
    )
    if not _owner._push_branch(
        context.spec,
        context.worktree,
        branch,
        force_with_lease=before_sha or None,
    ):
        _owner._park_failed_auto_rebase_push(context, before_sha, branch)
        return
    _owner._finalize_auto_rebase(context, branch, after_sha)
