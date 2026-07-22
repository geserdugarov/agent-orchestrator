# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating awaiting handler."""
from __future__ import annotations

from orchestrator.stages import _validating_state as _state
from orchestrator.stages import validating as _owner

_AwaitingValidation = _owner._AwaitingValidation
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
config = _owner.config
_OUTCOME_RETURN = _state._OUTCOME_RETURN


def _resume_validating_awaiting_dev(context: _AwaitingValidation) -> str:
    from orchestrator import workflow as _wf

    continue_action = (
        _wf._continue_command_action(context.comments, context.park_reason)
        if context.comments else "passthrough"
    )
    if continue_action == "refuse":
        _wf._refuse_parked_continue(context.gh, context.issue, context.state)
        context.gh.write_pinned_state(context.issue, context.state)
        return _OUTCOME_RETURN
    attempt = _owner._run_awaiting_dev(context, continue_action)
    if attempt is None:
        return _OUTCOME_RETURN
    context.state.set("last_agent_action_at", _wf._now_iso())
    if attempt.paused:
        return _OUTCOME_RETURN
    pushed = _owner._handle_dev_fix_result(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        attempt.run.worktree,
        attempt.run.agent_result,
        attempt.run.before_sha,
    )
    if not pushed:
        if not attempt.run.agent_result.interrupted:
            context.gh.write_pinned_state(context.issue, context.state)
        return _OUTCOME_RETURN
    _owner._bump_review_round(context.state)
    context.gh.write_pinned_state(context.issue, context.state)
    return _OUTCOME_RETURN


def _handle_validating_awaiting_human(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> str:
    """Route an awaiting-human `validating` tick after a park.

    A human replied (or a transient condition self-resolved) while the issue
    was parked. Resume the developer with their feedback -- identical mechanic
    to implementing's resume, but on a clean pushed fix we bump the round while
    staying on `validating` (no relabel emitted) so the reviewer re-evaluates
    the new head next tick. Docs are deferred to the final-docs handoff after
    reviewer approval.

    Returns ``"return"`` when the tick is fully handled (caller must return) or
    ``"spawn_reviewer"`` when the park cleared into a reviewer re-run (review-cap
    reset, reviewer timeout / silent crash) and the caller should fall through
    to the round-cap check and reviewer spawn.
    """
    from orchestrator import workflow as _wf

    context = _AwaitingValidation.build(gh, spec, issue, state)

    # Transient-park recovery: when the original park reason is something
    # that can resolve without a human comment (a push race that the
    # next --force-with-lease push will land, or an agent timeout that
    # the next tick can simply rerun past), re-attempt silently. This
    # mirrors the in_review recovery branch -- without it, the issue
    # would sit forever, because `_resume_developer_on_human_reply`
    # only fires on new issue-thread comments and the human action
    # that unstuck the underlying condition typically does not include
    # one.
    # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to
    # the `_sync_pr_worktree_to_base` retry loop -- the operator's
    # new comment is the "retry the rebase" signal, NOT a dev /
    # reviewer trigger for this stage. Stay silent so the refresh
    # keeps ownership of the comment; resuming the dev or
    # respawning the reviewer here would consume the comment as
    # input it has no context for and silently drop the retry
    # intent.
    if context.park_reason in _wf._AUTO_REBASE_PARK_REASONS:
        return _OUTCOME_RETURN
    # `/orchestrator add-review-rounds N` operator command. Only honored
    # on a `review_cap` park: the cap has consumed every review round and
    # plain resuming the dev would re-park on the same cap next tick (the
    # original bug -- the round bump in the resume branch just trips
    # `round_n >= MAX_REVIEW_ROUNDS` again). On other parks the human's
    # reply IS the input the dev / reviewer needs, so we don't intercept
    # it. On a non-command reply while parked on the cap we stay parked
    # silently rather than waking the dev on a do-nothing prompt.
    for decision_helper in (
        _owner._review_cap_awaiting_action,
        _owner._transient_awaiting_action,
        _owner._reviewer_retry_awaiting_action,
    ):
        action = decision_helper(context)
        if action is not None:
            return action
    return _owner._resume_validating_awaiting_dev(context)
