# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating drift result."""
from __future__ import annotations

from orchestrator.stages import _validating_state as _state
from orchestrator.stages import validating as _owner

_DevFixRun = _owner._DevFixRun
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
config = _owner.config
_OUTCOME_PARKED = _state._OUTCOME_PARKED
_OUTCOME_PUSHED = _state._OUTCOME_PUSHED
_OUTCOME_STUCK = _state._OUTCOME_STUCK
_PARK_REASON = _state._PARK_REASON
_PRE_DEV_FIX_SHA = _state._PRE_DEV_FIX_SHA
_REASON_AGENT_TIMEOUT = _state._REASON_AGENT_TIMEOUT
_REASON_PUSH_FAILED = _state._REASON_PUSH_FAILED
_REASON_REVIEWER_FAILED = _state._REASON_REVIEWER_FAILED
_REASON_REVIEWER_TIMEOUT = _state._REASON_REVIEWER_TIMEOUT
_REVIEW_ROUND = _state._REVIEW_ROUND


def _post_drift_ack(
    gh: GitHubClient, issue: Issue, state: PinnedState, reason: str,
) -> None:
    from orchestrator import workflow as _wf

    quoted = _wf._as_blockquote(reason)
    _wf._post_issue_comment(
        gh, issue, state,
        ":speech_balloon: dev session reports the existing work "
        f"satisfies the edit:\n\n{quoted}",
    )
    state.set("silent_park_count", 0)


def _dispose_user_content_change_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run: _DevFixRun,
) -> str:
    from orchestrator import workflow as _wf

    if run.agent_result.interrupted:
        return _OUTCOME_PARKED
    if run.agent_result.timed_out:
        _owner._park_dev_fix_timeout(gh, issue, state, run.before_sha)
        return _OUTCOME_PARKED
    if not _owner._dev_fix_is_publishable(spec, issue, state, run):
        ack_reason = _wf._drift_ack_reason(
            run.agent_result.last_message or "",
        )
        if ack_reason:
            _owner._post_drift_ack(gh, issue, state, ack_reason)
            return "ack"
        _wf._on_question(gh, issue, state, run.agent_result)
        return _OUTCOME_PARKED
    return (
        _OUTCOME_PUSHED if _owner._publish_dev_fix(gh, spec, issue, state, run)
        else _OUTCOME_PARKED
    )


def _post_user_content_change_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    *context_args,
) -> str:
    """Post-resume handling for a user-content-change dev resume.

    Returns one of:

    * ``"ack"`` -- the dev produced no commit but explicitly signaled
      acknowledgement via the `ACK: ...` marker emitted by
      `_build_user_content_change_prompt`. The reply is posted on the
      issue as an FYI and the handler does NOT park `awaiting_human`.
      Caller decides what to do with the label: validating stays put
      (the reviewer reruns on the current head); in_review bounces
      back to `validating` (the prior reviewer approval was for the
      old requirements, so the in_review HITL ready-ping must wait
      for a re-approval) WITHOUT spawning `documenting` -- no commit
      landed for the docs pass to react to.
    * ``"pushed"`` -- new commit landed and the push succeeded, OR this
      no-commit run found a committed-but-unpublished fix stranded on the
      branch by a prior parked / interrupted resume and published it (the
      stranded-fix gate, mirroring `_handle_dev_fix_result`).
      Validating stays on `validating` (and bumps `review_round`) so
      the reviewer re-evaluates the new head; in_review also hands
      straight back to `validating`. Docs are not run on this exit --
      the single docs pass is deferred to the final-docs handoff after
      reviewer approval. Any stale approval state must be reset by
      the caller before relabeling.
    * ``"parked"`` -- timeout, dirty tree, push fail, silent crash
      (empty `last_message`), OR a no-commit response WITHOUT the
      `ACK:` marker (treated as a clarification question via
      `_on_question`). State already carries the park flags. A
      shutdown-killed (interrupted) run also returns ``"parked"`` but
      WITHOUT setting any park flags or posting -- the run is ignored
      and the next tick retries the resume.

    The explicit `ACK:` marker is required because a generic non-empty
    no-commit response is often a clarification question, not an
    acknowledgement; swallowing it as an ack would post a misleading
    "existing work satisfies" comment AND continue the workflow with
    `awaiting_human=False`, stranding the real question.
    """
    state, run = _owner._dev_fix_run(context_args, {})
    return _owner._dispose_user_content_change_result(gh, spec, issue, state, run)


def _bump_review_round(state: PinnedState) -> None:
    current_round = int(state.get(_REVIEW_ROUND) or 0)
    state.set(_REVIEW_ROUND, current_round + 1)


def _recover_failed_push(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> str:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    if not worktree.exists():
        return _OUTCOME_STUCK
    branch = _wf._resolve_branch_name(state, spec, issue.number)
    if not _wf._push_branch(spec, worktree, branch):
        return _OUTCOME_STUCK
    _owner._bump_review_round(state)
    return _OUTCOME_PUSHED


def _recover_timed_out_fix(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> str:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    if not worktree.exists() or _wf._worktree_dirty_files(worktree):
        return _OUTCOME_STUCK
    before_sha = state.get(_PRE_DEV_FIX_SHA)
    if not isinstance(before_sha, str):
        return _OUTCOME_STUCK
    current_sha = _wf._head_sha(worktree)
    if not current_sha or current_sha == before_sha:
        state.set(_PRE_DEV_FIX_SHA, None)
        return "cleared"
    branch = _wf._resolve_branch_name(state, spec, issue.number)
    if not _wf._push_branch(spec, worktree, branch):
        return _OUTCOME_STUCK
    state.set(_PRE_DEV_FIX_SHA, None)
    _owner._bump_review_round(state)
    return _OUTCOME_PUSHED


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
    park_reason = state.get(_PARK_REASON)
    if park_reason == _REASON_PUSH_FAILED:
        return _owner._recover_failed_push(spec, issue, state)
    if park_reason in (_REASON_REVIEWER_TIMEOUT, _REASON_REVIEWER_FAILED):
        return "cleared"
    if park_reason == _REASON_AGENT_TIMEOUT:
        return _owner._recover_timed_out_fix(spec, issue, state)
    return _OUTCOME_STUCK
