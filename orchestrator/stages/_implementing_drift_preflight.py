# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing drift preflight."""
from __future__ import annotations

from orchestrator.stages import _implement_state as _state
from orchestrator.stages import implementing as _owner

_PreparedDevRun = _owner._PreparedDevRun
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
config = _owner.config
filter_trusted = _owner.filter_trusted
_AGENT_TIMEOUT = _state._AGENT_TIMEOUT
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID
_PARK_REASON = _state._PARK_REASON


def _handle_pre_session_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    if _wf._has_new_commits(spec, worktree):
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} issue body changed but the "
            "worktree carries unpushed commits from a previous tick "
            "and no dev session is recorded. Refusing to push commits "
            "that never saw the edited requirements; decide whether "
            "to discard the recovered work (reset the branch) and "
            "let a fresh agent run, or accept it as-is.",
            reason="stale_recovered_work",
        )
        gh.write_pinned_state(issue, state)
        return True
    if state.get(_AWAITING_HUMAN):
        _wf._post_issue_comment(
            gh, issue, state,
            ":pencil2: issue content changed; clearing the park and "
            "spawning a fresh dev run against the updated requirements.",
        )
        _wf._mark_drift_comments_consumed(gh, issue, state)
        state.set(_AWAITING_HUMAN, False)
        state.set(_PARK_REASON, None)
    return False


def _recover_quiet_implementer_timeout(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    if state.get(_PARK_REASON) != _AGENT_TIMEOUT:
        return False
    comments = filter_trusted(
        gh.comments_after(issue, state.get(_LAST_ACTION_COMMENT_ID))
    )
    if comments:
        return False
    if _owner._try_recover_implementing_timeout_park(gh, spec, issue, state) == "pushed":
        gh.write_pinned_state(issue, state)
    return True


def _prepare_awaiting_dev_run(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Optional[_PreparedDevRun]:
    from orchestrator import workflow as _wf

    if _owner._recover_quiet_implementer_timeout(gh, spec, issue, state):
        return None
    worktree = _owner._ensure_resume_worktree(spec, issue, state)
    before_sha = _wf._head_sha(worktree)
    resumed = _owner._resume_developer_on_human_reply(
        gh, spec, issue, state, pause_guard=True,
    )
    if resumed is None:
        return None
    worktree, agent_result, paused = resumed
    return _PreparedDevRun(agent_result, before_sha, paused, worktree)
