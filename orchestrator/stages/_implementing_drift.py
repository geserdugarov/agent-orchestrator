# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing drift."""
from __future__ import annotations

from orchestrator.stages import _implement_state as _state
from orchestrator.stages import implementing as _owner

_AgentWork = _owner._AgentWork
AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
PinnedState = _owner.PinnedState
Tuple = _owner.Tuple
config = _owner.config
dataclass = _owner.dataclass
_BRANCH = _state._BRANCH
_CODEX_SESSION_ID = _state._CODEX_SESSION_ID
_DEV_AGENT = _state._DEV_AGENT
_SILENT_PARK_COUNT = _state._SILENT_PARK_COUNT


def _handle_user_content_drift(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    new_hash: str,
) -> bool:
    """React to a human editing the issue title/body after the dev spawned.

    Persists the new content hash, then:
      * With a recorded dev session -> notify the human, mark the current
        conversation consumed, resume the locked session with the updated
        requirements, and dispose the result (publish a fresh commit, park a
        commit-less timeout, ACK an explicit "existing work satisfies" reply,
        or park the question). Always returns True -- the caller must return.
      * Without a dev session but with recovered unpushed commits from a prior
        tick -> park `stale_recovered_work` (those commits never saw the edited
        body) and return True.
      * Without a dev session and without recovered commits -> clear any park
        and return False so the caller falls through to the fresh-spawn path,
        which builds the implement prompt from the current `issue.body`.

    The issue spec ("don't re-decompose mid-implementation -- too disruptive")
    rules out routing back to `decomposing`; the locked session decides what to
    do with the new body instead.
    """
    state.set("user_content_hash", new_hash)
    if state.get(_DEV_AGENT) or state.get(_CODEX_SESSION_ID):
        _owner._resume_dev_on_implementing_drift(gh, spec, issue, state)
        return True
    return _owner._handle_pre_session_drift(gh, spec, issue, state)


@dataclass(frozen=True)
class _ImplementingDriftRun:
    worktree: Path
    agent_result: AgentResult
    before_sha: Optional[str]
    paused: bool
    committed: bool


def _run_implementing_drift_resume(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> _ImplementingDriftRun:
    from orchestrator import workflow as _wf

    worktree = _owner._ensure_resume_worktree(spec, issue, state)
    before_sha = _wf._head_sha(worktree)
    followup = _wf._build_user_content_change_prompt(
        issue, _wf._recent_comments_text(issue),
    )
    resumed = _owner._resume_dev_with_text(
        gh, spec, issue, state, followup, pause_guard=True,
    )
    return _owner._implementing_drift_run(before_sha, resumed)


def _implementing_drift_run(
    before_sha: Optional[str], resumed: Tuple[Path, AgentResult, bool],
) -> _ImplementingDriftRun:
    from orchestrator import workflow as _wf

    worktree, agent_result, paused = resumed
    after_sha = _wf._head_sha(worktree)
    return _ImplementingDriftRun(
        worktree=worktree,
        agent_result=agent_result,
        before_sha=before_sha,
        paused=paused,
        committed=bool(after_sha) and after_sha != before_sha,
    )


def _post_implementing_drift_ack(
    gh: GitHubClient, issue: Issue, state: PinnedState, reason: str,
) -> None:
    from orchestrator import workflow as _wf

    quoted = _owner._as_blockquote(reason)
    _wf._post_issue_comment(
        gh, issue, state,
        ":speech_balloon: dev session reports the existing "
        f"work satisfies the edit:\n\n{quoted}",
    )
    state.set(_SILENT_PARK_COUNT, 0)


def _dispose_implementing_drift(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    drift: _ImplementingDriftRun,
) -> None:
    from orchestrator import workflow as _wf

    if (
        _wf._ignore_if_interrupted(issue, drift.agent_result)
        or drift.paused
    ):
        return
    if drift.committed:
        _owner._publish_committed_work(
            gh, spec, issue, state,
            _AgentWork(drift.agent_result, drift.worktree),
        )
    elif drift.agent_result.timed_out:
        _owner._park_agent_timeout(gh, issue, state, drift.before_sha)
    else:
        ack_reason = _wf._drift_ack_reason(
            drift.agent_result.last_message or "",
        )
        if ack_reason:
            _owner._post_implementing_drift_ack(gh, issue, state, ack_reason)
        else:
            _owner._on_question(gh, issue, state, drift.agent_result)
    gh.write_pinned_state(issue, state)


def _resume_dev_on_implementing_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> None:
    from orchestrator import workflow as _wf

    _wf._post_issue_comment(
        gh, issue, state,
        ":pencil2: issue body changed; resuming dev session with "
        "the updated requirements.",
    )
    _wf._mark_drift_comments_consumed(gh, issue, state)
    drift = _owner._run_implementing_drift_resume(gh, spec, issue, state)
    state.set("last_agent_action_at", _wf._now_iso())
    state.set(_BRANCH, _wf._resolve_branch_name(state, spec, issue.number))
    _owner._dispose_implementing_drift(gh, spec, issue, state, drift)
