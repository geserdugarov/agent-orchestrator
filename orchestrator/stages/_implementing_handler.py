# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing handler."""
from __future__ import annotations

from orchestrator.stages import implementing as _owner

_AgentWork = _owner._AgentWork
_PreparedDevRun = _owner._PreparedDevRun
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
config = _owner.config


def _dispose_agent_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    prepared: _PreparedDevRun,
) -> None:
    """Dispose a completed implementing run and write pinned state.

    A timed-out run publishes a commit produced by THIS run (clean tree), parks
    a dirty tree for inspection, or parks `agent_timeout` when HEAD did not
    advance past `before_sha`. A clean exit publishes new commits or parks the
    agent's question. `before_sha` (not `_has_new_commits`, which only compares
    to `origin/<base>`) is what distinguishes a commit produced by THIS run
    from carried-over commits already on the branch.
    """
    from orchestrator import workflow as _wf

    if prepared.agent_result.timed_out:
        # The implementer can commit clean work and then get killed by the
        # timeout (or a descendant finishes the commit during cleanup). Don't
        # strand that commit behind `awaiting_human`: publish it if HEAD
        # advanced and the tree is clean, park a dirty tree for inspection, or
        # park as a timeout when it did not advance.
        after_sha = _wf._head_sha(prepared.worktree)
        if after_sha and after_sha != prepared.before_sha:
            _owner._publish_committed_work(
                gh,
                spec,
                issue,
                state,
                _AgentWork(prepared.agent_result, prepared.worktree),
            )
        else:
            _owner._park_agent_timeout(gh, issue, state, prepared.before_sha)
        gh.write_pinned_state(issue, state)
        return

    if _wf._has_new_commits(spec, prepared.worktree):
        _owner._publish_committed_work(
            gh,
            spec,
            issue,
            state,
            _AgentWork(prepared.agent_result, prepared.worktree),
        )
    else:
        _owner._on_question(gh, issue, state, prepared.agent_result)
    gh.write_pinned_state(issue, state)


def _implementing_preflight(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    from orchestrator import workflow as _wf

    if _wf._finalize_if_pr_merged(gh, spec, issue, state):
        return True
    if _wf._finalize_if_issue_closed(gh, spec, issue, state):
        return True
    if _owner._handle_stale_question_park(gh, spec, issue, state):
        return True
    if _owner._handle_parked_continue_command(gh, spec, issue, state):
        return True
    return False


def _handle_detected_implementing_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    from orchestrator import workflow as _wf

    new_hash = _wf._detect_user_content_change(gh, issue, state)
    return new_hash is not None and _owner._handle_user_content_drift(
        gh, spec, issue, state, new_hash,
    )


def _handle_implementing(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    if _owner._implementing_preflight(gh, spec, issue, state):
        return

    # User-content drift: a human edited the issue title/body after the dev
    # session was spawned. `_handle_user_content_drift` persists the new hash
    # and either resumes the locked session against the updated requirements
    # (returning True), parks recovered pre-edit work, or -- when no dev
    # session exists yet -- clears any park and returns False so the fresh-
    # spawn path below picks up the new body via `_build_implement_prompt`.
    if _owner._handle_detected_implementing_drift(gh, spec, issue, state):
        return

    prepared = _owner._prepare_dev_run(gh, spec, issue, state)
    if prepared is None:
        return

    state.set("last_agent_action_at", _wf._now_iso())

    # Shutdown-sweep interruption: a run the orchestrator killed mid-flight
    # has no trustworthy result, so ignore it and return WITHOUT writing
    # pinned state (the in-memory `awaiting_human=False` / watermark / session
    # mutations in `_prepare_dev_run` are discarded) so the next process
    # retries from durable state. Must precede the disposition below.
    if (
        _wf._ignore_if_interrupted(issue, prepared.agent_result)
        or prepared.paused
    ):
        return

    _owner._dispose_agent_result(gh, spec, issue, state, prepared)
