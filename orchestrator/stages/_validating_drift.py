# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating drift."""
from __future__ import annotations

from orchestrator.stages import _validating_state as _state
from orchestrator.stages import validating as _owner

AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Path = _owner.Path
PinnedState = _owner.PinnedState
config = _owner.config
dataclass = _owner.dataclass
_OUTCOME_PUSHED = _state._OUTCOME_PUSHED
_PARK_REASON = _state._PARK_REASON
_REASON_REVIEWER_FAILED = _state._REASON_REVIEWER_FAILED
_REASON_REVIEWER_TIMEOUT = _state._REASON_REVIEWER_TIMEOUT
_REASON_REVIEW_CAP = _state._REASON_REVIEW_CAP


@dataclass(frozen=True)
class _ValidatingDriftRun:
    worktree: Path
    agent_result: AgentResult
    before_sha: str
    paused: bool


def _run_validating_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> _ValidatingDriftRun:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    if not worktree.exists():
        worktree = _wf._ensure_worktree(
            spec,
            issue.number,
            branch=_wf._resolve_branch_name(state, spec, issue.number),
        )
    before_sha = _wf._head_sha(worktree)
    followup = _wf._build_user_content_change_prompt(
        issue, _wf._recent_comments_text(issue),
    )
    worktree, agent_result, paused = _wf._resume_dev_with_text(
        gh, spec, issue, state, followup, pause_guard=True,
    )
    return _ValidatingDriftRun(worktree, agent_result, before_sha, paused)


def _defer_validating_drift(state: PinnedState) -> bool:
    return bool(
        state.get("awaiting_human")
        and state.get(_PARK_REASON)
        in (_REASON_REVIEWER_TIMEOUT, _REASON_REVIEWER_FAILED, _REASON_REVIEW_CAP)
    )


def _finish_validating_drift(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run: _ValidatingDriftRun,
) -> None:
    outcome = _owner._post_user_content_change_result(
        gh,
        spec,
        issue,
        state,
        run.worktree,
        run.agent_result,
        run.before_sha,
    )
    if run.agent_result.interrupted:
        return
    if outcome == _OUTCOME_PUSHED:
        _owner._bump_review_round(state)
    gh.write_pinned_state(issue, state)


def _resume_dev_on_validating_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> bool:
    """Resume the dev session when a human edited the issue title/body while the
    reviewer was running.

    Re-decomposing now would discard the dev's already-pushed work, so notify
    the human, resume the dev session on its locked backend with the new body,
    and on a successful pushed fix bump `review_round` while staying on
    `validating` (no relabel emitted) so the reviewer re-evaluates the updated
    body + new diff on the next tick. An ACK reply (no commit) keeps the issue
    on `validating`. On a failed resume (timeout, dirty, no commit), the
    standard park flags land via `_post_user_content_change_result`.

    Returns True when a drift was detected and fully handled (caller must
    return). Returns False when there is no drift, or when the issue is parked
    with a reviewer-side reason (`reviewer_timeout` / `reviewer_failed`) or on
    the review-round cap (`review_cap`) -- those defer to the awaiting-human
    branch. A human "retry" comment on a reviewer-side park must re-spawn the
    REVIEWER, not the dev: the failure produced no review output for the dev to
    act on, and the reviewer re-reads the updated `issue.body` + comments via
    `_build_review_prompt` when it runs. For `review_cap`, the cap has consumed
    every round, so resuming the dev would re-park on the cap next tick; the
    operator's `/orchestrator add-review-rounds` command lives in the
    awaiting-human branch, and the command comment itself bumps the user-content
    hash, so without this bypass the drift block would fire first and the
    command would never be parsed. The new baseline hash is persisted here
    either way so the next tick's drift check has a stable comparison point.
    """
    from orchestrator import workflow as _wf

    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is None:
        return False
    state.set("user_content_hash", new_hash)
    if _owner._defer_validating_drift(state):
        return False

    _wf._post_issue_comment(
        gh, issue, state,
        ":pencil2: issue body changed; resuming dev session.",
    )
    # Mark the full issue thread as consumed: the dev sees it via
    # `_recent_comments_text` in the resume prompt, so the eventual
    # handoff to in_review must not replay those comments as fresh
    # feedback. Mirrors `_resume_developer_on_human_reply`'s pre-spawn bump.
    _wf._mark_drift_comments_consumed(gh, issue, state)
    run = _owner._run_validating_drift(gh, spec, issue, state)
    state.set("last_agent_action_at", _wf._now_iso())
    if run.paused:
        # Live pause applied during the drift resume: the helper already
        # stopped before persisting the session id or clearing
        # `awaiting_human`. Return WITHOUT running the result handler (which
        # would post / push / advance the round) or writing pinned state, so
        # the drift bookkeeping staged above stays unrecorded and the committed
        # work stays on the branch; the next tick re-detects the drift once the
        # label is removed.
        return True
    # Custom result handler: a no-commit-with-message reply is the dev
    # confirming the existing work already satisfies the edit, and the resume
    # prompt explicitly invites that response. `_handle_dev_fix_result` would
    # park on it via `_on_question`; use the user-content-specific helper so a
    # harmless clarification does not stall the issue.
    _owner._finish_validating_drift(gh, spec, issue, state, run)
    return True
