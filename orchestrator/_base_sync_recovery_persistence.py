# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Base sync recovery persistence."""
from __future__ import annotations

from orchestrator import _base_sync_state as _state
from orchestrator import base_sync as _owner

_AutoRebaseContext = _owner._AutoRebaseContext
_AutoRebaseRecoveryContext = _owner._AutoRebaseRecoveryContext
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
_AUTO_REBASE_PARK_REASONS = _state._AUTO_REBASE_PARK_REASONS
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_PARK_REASON = _state._PARK_REASON
_PENDING_PUSH_SHA = _state._PENDING_PUSH_SHA
_REVIEW_ROUND = _state._REVIEW_ROUND
log = _state.log


def _park_auto_rebase_failure(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    message: str,
    reason: str,
) -> None:
    """Park an issue awaiting human for an auto-rebase failure.

    Wraps `_park_awaiting_human` so every refresh-time failure mode
    parks identically: `awaiting_human=True`, the HITL message lands
    on the issue thread (NOT the PR -- the resume-on-human-reply
    scan reads from the issue), `last_action_comment_id` is ratcheted
    forward by `_park_awaiting_human`, and the durable
    `park_reason` is re-set after the helper clears it by contract.
    `gh.write_pinned_state` is called here so the caller can return
    immediately.

    `reason` must be one of `_AUTO_REBASE_PARK_REASONS` -- the refresh
    recovery branch keys off the same set to decide whether a new
    human comment on this issue is the "retry now" signal.
    """
    # Lazy import: `workflow` imports `base_sync` at module load time,
    # so a top-level `from . import workflow` would be a circular
    # import. Stage modules use the same late-bind pattern.
    from orchestrator import workflow as _wf
    assert reason in _AUTO_REBASE_PARK_REASONS, (
        f"_park_auto_rebase_failure called with reason={reason!r}, "
        f"which is not in _AUTO_REBASE_PARK_REASONS"
    )
    _wf._park_awaiting_human(gh, issue, state, message, reason=reason)
    state.set(_PARK_REASON, reason)
    gh.write_pinned_state(issue, state)


def _reset_clear_and_park(
    context: _AutoRebaseContext | _AutoRebaseRecoveryContext,
    reset_sha: str,
    *,
    message: str,
    reason: str,
    clean: bool = False,
) -> None:
    """Restore the worktree to `reset_sha`, drop the recovery anchor, and park.

    The shared tail of every auto-rebase park path: a rebase / push /
    recovery step could not safely finalize, so HEAD is hard-reset back
    to a known SHA (the pre-rebase anchor = the last-known remote PR
    head) so the same-tick stage handler dispatch never reads a local
    HEAD the PR may not carry, the crash-recovery anchor is cleared (the
    reset put HEAD back at it, so a follow-up tick would only hit the
    "HEAD == anchor" no-op case), and the issue is parked awaiting human.
    `clean=True` also runs `git clean -fd` after the reset to discard the
    untracked leftovers a dirty rebase produced (recoverable via
    `git reflog`).

    A failed reset / clean is logged but does not abort the park: the
    `awaiting_human` flag is what short-circuits the same-tick handlers,
    and it still lands even if the worktree is left on an unexpected SHA
    for the operator to inspect.
    """
    reset = _owner._git_hardened(
        "reset", "--hard", reset_sha, cwd=context.worktree,
    )
    if reset.returncode != 0:
        log.error(
            "issue=#%d auto-rebase recovery: reset --hard to %s failed: "
            "%s; the awaiting_human park still short-circuits same-tick "
            "handler dispatch but operator inspection of HEAD is needed",
            context.issue.number,
            reset_sha[:8],
            (reset.stderr or "").strip(),
        )
    if clean:
        cleaned = _owner._git_hardened("clean", "-fd", cwd=context.worktree)
        if cleaned.returncode != 0:
            log.error(
                "issue=#%d auto-rebase recovery: `git clean -fd` after "
                "the reset failed: %s",
                context.issue.number, (cleaned.stderr or "").strip(),
            )
    context.state.set(_PENDING_PUSH_SHA, None)
    _owner._park_auto_rebase_failure(
        context.gh,
        context.issue,
        context.state,
        message=message,
        reason=reason,
    )


def _prepare_recovered_rebase_state(
    context: _AutoRebaseRecoveryContext,
) -> None:
    """Clear the recovery anchor and commit any pending human retry."""
    if context.unparking_consumed_max is not None:
        context.state.set(
            "last_action_comment_id", context.unparking_consumed_max,
        )
        context.state.set(_AWAITING_HUMAN, False)
        context.state.set(_PARK_REASON, None)
    context.state.set(_PENDING_PUSH_SHA, None)
    context.state.set(_REVIEW_ROUND, 0)


def _post_recovered_rebase_notice(
    context: _AutoRebaseRecoveryContext, notice: str,
) -> None:
    """Post the recovery notice without blocking state finalization."""
    try:
        _owner._post_pr_comment(
            context.gh, context.pr_number, context.state, notice,
        )
    except Exception:
        log.exception(
            "issue=#%s could not post auto-rebase recovery notice to "
            "PR #%s", context.issue.number, context.pr_number,
        )


def _emit_recovered_rebase_event(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
    method: str,
) -> None:
    """Emit the stable audit shape for a recovered auto-rebase."""
    context.gh.emit_event(
        "base_rebased",
        issue_number=context.issue.number,
        stage=context.label,
        pr_number=context.pr_number,
        sha=local_head,
        method=method,
        review_round=0,
        retry_count=context.state.get("retry_count"),
    )


def _route_recovered_rebase(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
    method: str,
) -> bool:
    """Persist recovery progress and route only a current head to validation."""
    if context.behind == 0:
        log.info(
            "issue=#%d auto-rebase recovery (%s): recovered head %s is "
            "current; routing %r -> validating",
            context.issue.number,
            method,
            local_head[:8],
            context.label,
        )
        context.gh.set_workflow_label(context.issue, "validating")
        context.gh.write_pinned_state(context.issue, context.state)
        return True
    context.gh.write_pinned_state(context.issue, context.state)
    log.info(
        "issue=#%d auto-rebase recovery (%s): recovered head %s is still "
        "%d commit(s) behind %s/%s; falling through to the normal rebase "
        "+ push flow",
        context.issue.number,
        method,
        local_head[:8],
        context.behind,
        context.spec.remote_name,
        context.spec.base_branch,
    )
    return False


def _finalize_recovered_rebase(
    context: _AutoRebaseRecoveryContext,
    *,
    local_head: str,
    method: str,
    notice: str,
) -> bool:
    """Finalize a recovered push and route it according to current base lag."""
    _owner._prepare_recovered_rebase_state(context)
    _owner._post_recovered_rebase_notice(context, notice)
    _owner._emit_recovered_rebase_event(context, local_head, method)
    return _owner._route_recovered_rebase(context, local_head, method)
