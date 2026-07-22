# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing spawn."""
from __future__ import annotations

from orchestrator.stages import _implement_state as _state
from orchestrator.stages import implementing as _owner

_DevSession = _owner._DevSession
_PreparedDevRun = _owner._PreparedDevRun
AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
PinnedState = _owner.PinnedState
config = _owner.config
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_BRANCH = _state._BRANCH
_DEV_AGENT = _state._DEV_AGENT
_DEV_RESUME_COUNT = _state._DEV_RESUME_COUNT
_DEV_SESSION_ID = _state._DEV_SESSION_ID
_IMPLEMENTING_STAGE = _state._IMPLEMENTING_STAGE
_RETRY_COUNT = _state._RETRY_COUNT


def _recovered_dev_result(state: PinnedState) -> AgentResult:
    return AgentResult(
        session_id=_owner._read_dev_session(state)[3],
        last_message="(orchestrator restart: pushing previously committed work)",
        exit_code=0,
        timed_out=False,
        stdout="",
        stderr="",
    )


def _spawn_implementer(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    worktree: Path,
) -> Optional[tuple[AgentResult, bool]]:
    from orchestrator import workflow as _wf

    if not _owner._check_and_increment_retry_budget(gh, issue, state):
        gh.write_pinned_state(issue, state)
        return None
    session = _DevSession(*_owner._read_dev_session(state))
    state.set(_DEV_AGENT, session.spec)
    agent_result = _wf._run_agent_tracked(
        gh,
        issue.number,
        agent_role="developer",
        stage=_IMPLEMENTING_STAGE,
        backend=session.backend,
        prompt=_wf._build_implement_prompt(
            spec,
            issue,
            _wf._recent_comments_text(issue),
            config.default_repo_specs(),
        ),
        cwd=worktree,
        agent_spec=session.spec,
        extra_args=session.extra_args,
        review_round=state.get("review_round", 0),
        retry_count=state.get(_RETRY_COUNT),
    )
    _wf._accumulate_issue_usage(state, agent_result.usage)
    if agent_result.session_id:
        state.set(_DEV_SESSION_ID, agent_result.session_id)
        state.set(_DEV_RESUME_COUNT, 0)
    return agent_result, _wf._paused_during_agent_run(gh, issue)


def _prepare_active_dev_run(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Optional[_PreparedDevRun]:
    from orchestrator import workflow as _wf

    worktree = _wf._ensure_worktree(
        spec,
        issue.number,
        branch=_wf._resolve_branch_name(state, spec, issue.number),
    )
    before_sha = _wf._head_sha(worktree)
    if _wf._has_new_commits(spec, worktree):
        _wf.log.info(
            "issue=#%d skipping agent; worktree already has commits",
            issue.number,
        )
        return _PreparedDevRun(
            _owner._recovered_dev_result(state), before_sha, False, worktree,
        )
    spawned = _owner._spawn_implementer(gh, spec, issue, state, worktree)
    if spawned is None:
        return None
    agent_result, paused = spawned
    return _PreparedDevRun(agent_result, before_sha, paused, worktree)


def _prepare_dev_run(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> Optional[_PreparedDevRun]:
    """Set up and run (or recover) the dev agent for one implementing tick.

    Returns a prepared run for the caller to dispose, or None
    when the tick is already complete and the caller must return:
      * awaiting-human with an `agent_timeout` park and no human reply -> a
        silent `_try_recover_implementing_timeout_park` attempt (state written
        here on "pushed", left parked on "stuck");
      * awaiting-human resume with no new comments -> nothing to do;
      * a fresh spawn blocked by the 24h retry cap (parked, state written).

    `before_sha` is the pre-agent HEAD watermark the timeout disposition uses
    to tell a commit produced by THIS run from carried-over commits already on
    the branch.
    """
    from orchestrator import workflow as _wf

    if state.get(_AWAITING_HUMAN):
        prepared = _owner._prepare_awaiting_dev_run(gh, spec, issue, state)
    else:
        prepared = _owner._prepare_active_dev_run(gh, spec, issue, state)
    if prepared is not None:
        state.set(
            _BRANCH, _wf._resolve_branch_name(state, spec, issue.number),
        )
    return prepared
