# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Documenting preconditions."""
from __future__ import annotations

from orchestrator.stages import _documenting_state as _state
from orchestrator.stages import documenting as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
config = _owner.config
filter_trusted = _owner.filter_trusted
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID
_PARK_REASON = _state._PARK_REASON


def _finalize_documenting_terminal(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    """Terminal issue/PR short-circuits before the docs pass runs.

    External merge: if the PR was merged before the docs pass ran,
    finalize to `done` rather than fetching the branch and running the
    documenting agent against an already-landed PR. Closed-issue
    counterpart: the closed-`documenting` sweep yields issues a human
    closed without a merged PR -- flip to `rejected` so the docs agent
    does not run against a closed issue.

    Returns True when the issue was routed to a terminal state and the
    caller must return.
    """
    from orchestrator import workflow as _wf

    if _wf._finalize_if_pr_merged(gh, spec, issue, state):
        return True
    if _wf._finalize_if_issue_closed(gh, spec, issue, state):
        return True
    return False


def _park_documenting_without_pr(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Park a `documenting` issue that has no pinned `pr_number`.

    Documenting only runs against an existing PR worktree. Without a
    pinned `pr_number` we cannot anchor on the dev's branch and must not
    branch off the base (that would orphan the docs commit from the
    implementing PR). Park once and let the operator relabel; idempotency
    by `awaiting_human` mirrors `_handle_in_review`'s missing-pr-number
    guard.
    """
    from orchestrator import workflow as _wf

    if state.get(_AWAITING_HUMAN):
        return
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `documenting` without a pinned "
        "`pr_number`; the documenting stage runs against an existing "
        "PR worktree. Relabel back to `implementing` (the dev's PR "
        "opens there) after fixing.",
        reason="missing_pr_number",
    )
    gh.write_pinned_state(issue, state)


def _documenting_parked_no_input(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> bool:
    """Already-parked, no-new-input fast path.

    When `awaiting_human` is set and no human comment has arrived since
    the park (and drift did not clear the flag), there is nothing to act
    on. Skip the fetch + ahead/behind check entirely so a transient
    failure mode (fetch_failed / diverged_branch) does NOT re-post its
    park comment every tick -- non-recoverable parks (agent_question /
    dirty_worktree / agent_silent) likewise stay silent until a human
    reply. Validating uses the same shape via its transient-park recovery
    branch; documenting has no transient recovery yet, so the early
    return alone is enough.

    Returns True when the issue is parked with nothing to act on (the
    caller must return), False to proceed with the normal docs flow.
    """
    from orchestrator import workflow as _wf

    if not state.get(_AWAITING_HUMAN):
        return False
    # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to the
    # `_sync_pr_worktree_to_base` retry loop -- the operator's new comment
    # is the "retry the rebase" signal, NOT a documenting-stage trigger.
    # Stay silent so the refresh keeps ownership of the comment.
    if state.get(_PARK_REASON) in _wf._AUTO_REBASE_PARK_REASONS:
        return True
    last_action_id = state.get(_LAST_ACTION_COMMENT_ID)
    # Only a trusted reply wakes a parked docs pass: with `ALLOWED_ISSUE_AUTHORS`
    # set an outsider comment must read as silence so the park survives instead
    # of falling through to the docs resume in `_run_documenting_dev`.
    if not filter_trusted(gh.comments_after(issue, last_action_id)):
        return True
    return False


def _refuse_parked_continue_command(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> bool:
    """Refuse a content-free `/orchestrator continue` on a `documenting` park
    that needs real human guidance, BEFORE the drift / resume paths.

    Documenting has no preserved feedback batch to replay, so a bare continue
    resolves to just two shapes: a retryable session-failure park
    (`agent_silent` / `agent_timeout`) whose awaiting-human resume reruns the
    FULL documentation prompt, and a park that needs a real answer. A bare
    continue no longer shifts `user_content_hash`, so `_reconcile_documenting_drift`
    stays silent and the retry falls through to `_run_documenting_dev`'s resume
    (issue #729) -- only the refusal needs interception here.

    Returns True when a content-free continue on a non-retryable park was
    refused (command consumed, note posted, state written) and the caller must
    return. Returns False to fall through: not parked, an auto-rebase park (the
    refresh loop owns the nudge), no new comment, no bare continue, a retryable
    park, or a command posted alongside genuine guidance.
    """
    from orchestrator import workflow as _wf

    if not state.get(_AWAITING_HUMAN):
        return False
    park_reason = state.get(_PARK_REASON)
    if park_reason in _wf._AUTO_REBASE_PARK_REASONS:
        return False
    new_comments = filter_trusted(
        gh.comments_after(issue, state.get(_LAST_ACTION_COMMENT_ID))
    )
    if not new_comments:
        return False
    if _wf._continue_command_action(new_comments, park_reason) != "refuse":
        return False
    _wf._refuse_parked_continue(gh, issue, state)
    gh.write_pinned_state(issue, state)
    return True


def _documenting_preconditions_handled(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
    pr_number,
) -> bool:
    """Run the pre-context guards; True when the tick is already resolved.

    Covers PR-state terminals, a `documenting` label with no pinned
    `pr_number`, and an operator `/orchestrator continue` refused on a park
    that needs real guidance. A bare continue does not shift
    `user_content_hash`, so the retryable resume later reruns the docs prompt
    without a spurious drift notice. See `_refuse_parked_continue_command`.
    """
    if _owner._finalize_documenting_terminal(gh, spec, issue, state):
        return True
    if pr_number is None:
        _owner._park_documenting_without_pr(gh, issue, state)
        return True
    return _owner._refuse_parked_continue_command(gh, issue, state)
