# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing recovery."""
from __future__ import annotations

from orchestrator.stages import _implement_state as _state
from orchestrator.stages import implementing as _owner

_AgentWork = _owner._AgentWork
AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
config = _owner.config
_AGENT_TIMEOUT = _state._AGENT_TIMEOUT
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_PARK_REASON = _state._PARK_REASON
_PRE_IMPLEMENT_SHA = _state._PRE_IMPLEMENT_SHA
_REASON_STUCK = _state._REASON_STUCK


def _publish_committed_work(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    work: _AgentWork,
) -> None:
    """Publish a worktree that carries a new commit.

    A clean tree pushes/opens the PR via `_on_commits`; a tree with
    uncommitted edits parks via `_on_dirty_worktree` (pushing would publish a
    branch that omits the dirty files). Shared by the fresh-completion, timeout,
    and user-content-drift dispositions so each handles a committed worktree
    identically.
    """
    from orchestrator import workflow as _wf

    dirty = _wf._worktree_dirty_files(work.worktree)
    if dirty:
        _owner._on_dirty_worktree(gh, issue, state, work.agent_result, dirty)
    else:
        _owner._on_commits(gh, spec, issue, state, work.agent_result)


def _park_agent_timeout(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    before_sha: Optional[str],
) -> None:
    """Park an implementer timeout that produced no publishable commit.

    Tags the park `agent_timeout` and persists the pre-agent SHA so the
    next-tick recovery (`_try_recover_implementing_timeout_park`) can publish a
    commit a lingering descendant finishes after this point without waiting for
    a human reply.
    """
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent timed out after "
        f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
        reason=_AGENT_TIMEOUT,
    )
    state.set(_PARK_REASON, _AGENT_TIMEOUT)
    state.set(_PRE_IMPLEMENT_SHA, before_sha or "")


def _try_recover_implementing_timeout_park(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> str:
    """Quietly publish a clean commit stranded by an implementer timeout.

    Implementing-stage counterpart to validating's
    `_try_recover_validating_transient_park`. An `agent_timeout` park can
    still carry a clean commit: a descendant the timeout cleanup raced
    finished writing it after disposition (the #77 shape, where the commit
    timestamp landed after the timeout event). Republish it through the
    normal commit path so a human does not have to manually clear
    `awaiting_human` to unstick the issue.

    Returns:
      * ``"pushed"`` -- a clean commit advanced past `pre_implement_sha` and
        was published via `_on_commits` (branch pushed, PR opened/reused,
        label -> validating, park flags cleared). Caller writes state.
      * ``"stuck"`` -- nothing safely recoverable (worktree reaped, dirty
        tree, missing watermark, or HEAD unchanged). Caller stays parked.

    Unlike validating's silent reviewer-rerun recovery this DOES post the
    normal ":sparkles: PR opened" comment via `_on_commits` -- publishing the
    branch is the entire point of the recovery. It must not spawn the agent.
    """
    from orchestrator import workflow as _wf

    wt = _wf._worktree_path(spec, issue.number)
    if not wt.exists():
        # Worktree reaped: the local commit is gone, nothing to publish.
        return _REASON_STUCK
    if _wf._worktree_dirty_files(wt):
        # A descendant left uncommitted edits; pushing would publish an
        # incomplete branch. Stay parked for human inspection.
        return _REASON_STUCK
    pre_sha = state.get(_PRE_IMPLEMENT_SHA)
    if not isinstance(pre_sha, str):
        # The timeout-tagging path always persists this; a missing watermark
        # is foreign state we cannot reason about, so stay parked rather than
        # risk publishing a branch we cannot vouch for.
        return _REASON_STUCK
    now_sha = _wf._head_sha(wt)
    if not now_sha or now_sha == pre_sha:
        # The timeout produced no new commit; stay parked for a human reply.
        return _REASON_STUCK
    # A clean commit advanced past the pre-timeout SHA. Clear the park flags
    # and publish it through the normal commit path.
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)
    state.set(_PRE_IMPLEMENT_SHA, None)
    _, _, _, dev_sid = _owner._read_dev_session(state)
    agent_result = AgentResult(
        session_id=dev_sid,
        last_message=(
            "(orchestrator recovery: publishing commit produced around the "
            "agent timeout)"
        ),
        exit_code=0,
        timed_out=False,
        stdout="",
        stderr="",
    )
    _owner._on_commits(gh, spec, issue, state, agent_result)
    return "pushed"
