# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a finished dev run leaves behind, and the timeout's second chance.

`before_sha` -- the pre-agent HEAD -- is what these four share. A timed-out run
is not automatically a failure: the implementer can commit clean work and then
be killed by the timeout, or a descendant can finish the commit during cleanup,
so the disposition asks whether HEAD MOVED rather than whether the run exited
well. `_has_new_commits` cannot answer that (it compares against
`origin/<base>`, so carried-over commits from an earlier tick look identical),
which is the whole reason the watermark is threaded this far.

When HEAD did not move, the park persists that same watermark as
`pre_implement_sha`, and that is why the recovery lives here rather than beside
the other preflight checks: it is the only reader of what the park wrote, and it
republishes through the normal commit path -- the ":sparkles: PR opened" comment
included -- because publishing the branch IS the recovery. It must never spawn
an agent, and it stays parked on anything it cannot vouch for: a reaped
worktree, a dirty tree, a missing watermark, or an unmoved HEAD.

Both dispositions route a committed worktree through one place, so a dirty tree
refuses the push identically whether it came from a clean exit, a timeout, or a
drift resume.
"""
from __future__ import annotations

from typing import Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.git.verification import probes as _verification_probes
from orchestrator.git.worktrees import (
    creation as _worktree_creation,
    paths as _worktree_paths,
)
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.stages.implementing import (
    models as _models,
    parks as _parks,
    publication as _publication,
    session_read as _session_read,
    state as _state,
)


def _publish_committed_work(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    work: _models._AgentWork,
) -> None:
    """Publish a worktree that carries a new commit.

    A clean tree pushes/opens the PR via `_on_commits`; a tree with
    uncommitted edits parks via `_on_dirty_worktree` (pushing would publish a
    branch that omits the dirty files). Shared by the fresh-completion, timeout,
    and user-content-drift dispositions so each handles a committed worktree
    identically.
    """
    dirty = _verification_probes._worktree_dirty_files(work.worktree)
    if dirty:
        _parks._on_dirty_worktree(gh, issue, state, work.agent_result, dirty)
    else:
        _publication._on_commits(gh, spec, issue, state, work.agent_result)


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
    _guards._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent timed out after "
        f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
        reason=_state._AGENT_TIMEOUT,
    )
    state.set(_state._PARK_REASON, _state._AGENT_TIMEOUT)
    state.set(_state._PRE_IMPLEMENT_SHA, before_sha or "")


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
    wt = _worktree_paths._worktree_path(spec, issue.number)
    if not wt.exists():
        # Worktree reaped: the local commit is gone, nothing to publish.
        return _state._REASON_STUCK
    if _verification_probes._worktree_dirty_files(wt):
        # A descendant left uncommitted edits; pushing would publish an
        # incomplete branch. Stay parked for human inspection.
        return _state._REASON_STUCK
    pre_sha = state.get(_state._PRE_IMPLEMENT_SHA)
    if not isinstance(pre_sha, str):
        # The timeout-tagging path always persists this; a missing watermark
        # is foreign state we cannot reason about, so stay parked rather than
        # risk publishing a branch we cannot vouch for.
        return _state._REASON_STUCK
    now_sha = _verification_probes._head_sha(wt)
    if not now_sha or now_sha == pre_sha:
        # The timeout produced no new commit; stay parked for a human reply.
        return _state._REASON_STUCK
    # A clean commit advanced past the pre-timeout SHA. Clear the park flags
    # and publish it through the normal commit path.
    state.set(_state._AWAITING_HUMAN, False)
    state.set(_state._PARK_REASON, None)
    state.set(_state._PRE_IMPLEMENT_SHA, None)
    _, _, _, dev_sid = _session_read._read_dev_session(state)
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
    _publication._on_commits(gh, spec, issue, state, agent_result)
    return "pushed"


def _dispose_agent_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    prepared: _models._PreparedDevRun,
) -> None:
    """Dispose a completed implementing run and write pinned state.

    A timed-out run publishes a commit produced by THIS run (clean tree), parks
    a dirty tree for inspection, or parks `agent_timeout` when HEAD did not
    advance past `before_sha`. A clean exit publishes new commits or parks the
    agent's question. `before_sha` (not `_has_new_commits`, which only compares
    to `origin/<base>`) is what distinguishes a commit produced by THIS run
    from carried-over commits already on the branch.
    """
    if prepared.agent_result.timed_out:
        # The implementer can commit clean work and then get killed by the
        # timeout (or a descendant finishes the commit during cleanup). Don't
        # strand that commit behind `awaiting_human`: publish it if HEAD
        # advanced and the tree is clean, park a dirty tree for inspection, or
        # park as a timeout when it did not advance.
        after_sha = _verification_probes._head_sha(prepared.worktree)
        if after_sha and after_sha != prepared.before_sha:
            _publish_committed_work(
                gh,
                spec,
                issue,
                state,
                _models._AgentWork(prepared.agent_result, prepared.worktree),
            )
        else:
            _park_agent_timeout(gh, issue, state, prepared.before_sha)
        gh.write_pinned_state(issue, state)
        return

    if _worktree_creation._has_new_commits(spec, prepared.worktree):
        _publish_committed_work(
            gh,
            spec,
            issue,
            state,
            _models._AgentWork(prepared.agent_result, prepared.worktree),
        )
    else:
        _parks._on_question(gh, issue, state, prepared.agent_result)
    gh.write_pinned_state(issue, state)
