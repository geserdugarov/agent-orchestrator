# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a mid-implementation edit to the requirements is answered with.

Re-decomposing is off the table once code exists -- too disruptive -- so the
locked dev session decides what the new body means instead. The new hash is
persisted first, unconditionally, because a tick that resumed the dev and then
failed to record the hash would resume it again on the next poll for the same
edit. Then the session is what routes it: with one recorded, the human is told,
the conversation so far is marked consumed, and the session is resumed with the
updated requirements quoted; without one, the pre-session path answers instead.

The disposition is wider than a normal run's because an edit has a fourth
possible answer. A fresh commit publishes, a commit-less timeout parks, a
question parks -- and a dev that replies "the existing work already satisfies
this" is ACKed rather than parked, which is what keeps a no-op edit from sitting
awaiting a human who has nothing left to say. That ACK also clears the
silent-park streak: the session answered, so it is not the poisoned one the
streak is counting.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from github.Issue import Issue

from orchestrator import agents as _agents, config
from orchestrator.git.verification import probes as _verification_probes
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.github import client as _client, pinned_state as _pinned_state
from orchestrator.workflow.engine import (
    comments as _comments,
    drift as _engine_drift,
    guards as _guards,
    messages as _messages,
    usage as _usage,
)
from orchestrator.workflow.stages.implementing import (
    disposition as _disposition,
    drift_preflight as _drift_preflight,
    models as _models,
    parks as _parks,
    resume as _resume,
    session_read as _session_read,
    state as _state,
    worktree as _worktree,
)


def _handle_user_content_drift(
    gh: _client.GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: _pinned_state.PinnedState,
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
    if state.get(_state._DEV_AGENT) or state.get(_state._CODEX_SESSION_ID):
        _resume_dev_on_implementing_drift(gh, spec, issue, state)
        return True
    return _drift_preflight._handle_pre_session_drift(gh, spec, issue, state)


@dataclass(frozen=True)
class _ImplementingDriftRun:
    worktree: Path
    agent_result: _agents.AgentResult
    before_sha: Optional[str]
    paused: bool
    committed: bool


def _run_implementing_drift_resume(
    gh: _client.GitHubClient, spec: config.RepoSpec, issue: Issue, state: _pinned_state.PinnedState,
) -> _ImplementingDriftRun:
    worktree = _worktree._ensure_resume_worktree(spec, issue, state)
    before_sha = _verification_probes._head_sha(worktree)
    followup = _engine_drift._build_user_content_change_prompt(
        issue, _comments._recent_comments_text(issue),
    )
    resumed = _resume._resume_dev_with_text(
        gh, spec, issue, state, followup, pause_guard=True,
    )
    return _implementing_drift_run(before_sha, resumed)


def _implementing_drift_run(
    before_sha: Optional[str], resumed: Tuple[Path, _agents.AgentResult, bool],
) -> _ImplementingDriftRun:
    worktree, agent_result, paused = resumed
    after_sha = _verification_probes._head_sha(worktree)
    return _ImplementingDriftRun(
        worktree=worktree,
        agent_result=agent_result,
        before_sha=before_sha,
        paused=paused,
        committed=bool(after_sha) and after_sha != before_sha,
    )


def _post_implementing_drift_ack(
    gh: _client.GitHubClient, issue: Issue, state: _pinned_state.PinnedState, reason: str,
) -> None:
    quoted = _session_read._as_blockquote(reason)
    _comments._post_issue_comment(
        gh, issue, state,
        ":speech_balloon: dev session reports the existing "
        f"work satisfies the edit:\n\n{quoted}",
    )
    state.set(_state._SILENT_PARK_COUNT, 0)


def _dispose_implementing_drift(
    gh: _client.GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: _pinned_state.PinnedState,
    drift: _ImplementingDriftRun,
) -> None:
    if (
        _guards._ignore_if_interrupted(issue, drift.agent_result)
        or drift.paused
    ):
        return
    if drift.committed:
        _disposition._publish_committed_work(
            gh, spec, issue, state,
            _models._AgentWork(drift.agent_result, drift.worktree),
        )
    elif drift.agent_result.timed_out:
        _disposition._park_agent_timeout(gh, issue, state, drift.before_sha)
    else:
        ack_reason = _messages._drift_ack_reason(
            drift.agent_result.last_message or "",
        )
        if ack_reason:
            _post_implementing_drift_ack(gh, issue, state, ack_reason)
        else:
            _parks._on_question(gh, issue, state, drift.agent_result)
    gh.write_pinned_state(issue, state)


def _resume_dev_on_implementing_drift(
    gh: _client.GitHubClient, spec: config.RepoSpec, issue: Issue, state: _pinned_state.PinnedState,
) -> None:
    _comments._post_issue_comment(
        gh, issue, state,
        ":pencil2: issue body changed; resuming dev session with "
        "the updated requirements.",
    )
    _engine_drift._mark_drift_comments_consumed(gh, issue, state)
    drift = _run_implementing_drift_resume(gh, spec, issue, state)
    state.set("last_agent_action_at", _usage._now_iso())
    state.set(
        _state._BRANCH,
        _worktree_paths._resolve_branch_name(state, spec, issue.number),
    )
    _dispose_implementing_drift(gh, spec, issue, state, drift)
