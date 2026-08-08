# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Deciding what one implementing tick actually runs, if anything.

The first question is not which prompt to build -- it is whether the issue is
parked. An awaiting-human tick belongs to the human's reply (or to the quiet
timeout recovery); only an unparked one is allowed to spawn.

An unparked tick still has a shortcut before the agent: a worktree that already
carries commits is a previous run whose publication was interrupted, so the
recovered result is synthesized and the commits are pushed rather than
implemented again. Then the retry budget gates the spawn, and the agent spec is
persisted BEFORE the run -- so a spawn that commits but returns no session id
still leaves the durable role identity behind and a later `DEV_AGENT` flip
cannot retarget the next resume at a backend that never ran on this issue.

`before_sha` is captured on every path, including the shortcut, because the
disposition downstream distinguishes a commit produced by THIS run from
carried-over work by comparing against it. The branch is persisted on every
prepared run for the same reason: whatever the next tick resolves has to be the
branch this one worked on.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator._workflow_state import log
from orchestrator.agents import AgentResult
from orchestrator.git.verification import probes as _verification_probes
from orchestrator.git.worktrees import (
    creation as _worktree_creation,
    paths as _worktree_paths,
)
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import (
    comments as _comments,
    guards as _guards,
    prompts as _prompts,
    usage as _usage,
)
from orchestrator.workflow.stages.implementing import (
    drift_preflight as _drift_preflight,
    models as _models,
    session as _session,
    session_read as _session_read,
    state as _state,
)


def _recovered_dev_result(state: PinnedState) -> AgentResult:
    return AgentResult(
        session_id=_session_read._read_dev_session(state)[3],
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
    if not _session._check_and_increment_retry_budget(gh, issue, state):
        gh.write_pinned_state(issue, state)
        return None
    session = _models._DevSession(*_session_read._read_dev_session(state))
    state.set(_state._DEV_AGENT, session.spec)
    agent_result = _usage._run_agent_tracked(
        gh,
        issue.number,
        agent_role="developer",
        stage=_state._IMPLEMENTING_STAGE,
        backend=session.backend,
        prompt=_prompts._build_implement_prompt(
            spec,
            issue,
            _comments._recent_comments_text(issue),
            config.default_repo_specs(),
        ),
        cwd=worktree,
        agent_spec=session.spec,
        extra_args=session.extra_args,
        review_round=state.get("review_round", 0),
        retry_count=state.get(_state._RETRY_COUNT),
    )
    _usage._accumulate_issue_usage(state, agent_result.usage)
    if agent_result.session_id:
        state.set(_state._DEV_SESSION_ID, agent_result.session_id)
        state.set(_state._DEV_RESUME_COUNT, 0)
    return agent_result, _guards._paused_during_agent_run(gh, issue)


def _prepare_active_dev_run(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Optional[_models._PreparedDevRun]:
    worktree = _worktree_creation._ensure_worktree(
        spec,
        issue.number,
        branch=_worktree_paths._resolve_branch_name(state, spec, issue.number),
    )
    before_sha = _verification_probes._head_sha(worktree)
    if _worktree_creation._has_new_commits(spec, worktree):
        log.info(
            "issue=#%d skipping agent; worktree already has commits",
            issue.number,
        )
        return _models._PreparedDevRun(
            _recovered_dev_result(state), before_sha, False, worktree,
        )
    spawned = _spawn_implementer(gh, spec, issue, state, worktree)
    if spawned is None:
        return None
    agent_result, paused = spawned
    return _models._PreparedDevRun(agent_result, before_sha, paused, worktree)


def _prepare_dev_run(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> Optional[_models._PreparedDevRun]:
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
    if state.get(_state._AWAITING_HUMAN):
        prepared = _drift_preflight._prepare_awaiting_dev_run(gh, spec, issue, state)
    else:
        prepared = _prepare_active_dev_run(gh, spec, issue, state)
    if prepared is not None:
        state.set(
            _state._BRANCH,
            _worktree_paths._resolve_branch_name(state, spec, issue.number),
        )
    return prepared
