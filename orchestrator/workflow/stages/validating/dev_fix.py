# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What one finished dev fix leaves behind, whichever route started it.

The reviewer feedback route, the awaiting-human resume, and the drift resume
all end here, because the questions after a dev run are the same three
regardless of what prompted it: did the run produce something publishable, is
the tree clean enough to push, and does the reviewer owe the branch another
look. Only the disposition order differs, and `_dispose_dev_fix_result` fixes
it -- an interrupted run first, so a shutdown-killed agent parks nothing and
the next tick simply retries it, then the timeout park, then the question.

`_stranded_fix_unpushed` is the non-obvious gate. A fix committed by an
earlier run that parked before publishing looks identical to "the agent did
nothing" on every later resume -- `after_sha == before_sha` -- so without it
the commit can never reach the PR and the issue ping-pongs between
awaiting-human parks forever. It is conservative by construction: a dirty
tree, a failed fetch, or a remote that moved all report False, because
pushing over a head nobody reconciled is worse than one more park.

`_bump_review_round` is the counter every landed fix pays into. It stays here
rather than beside any one caller because all three routes owe it for the
same reason -- the head the reviewer approved or rejected no longer exists,
so the round it spent does not count against the cap.
"""
from __future__ import annotations

from pathlib import Path

from github.Issue import Issue

from orchestrator import config
from orchestrator.git import authentication as _authentication
from orchestrator.git.publication import probes as _publication_probes
from orchestrator.git.verification import probes as _verification_probes
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.stages.implementing import parks as _dev_parks
from orchestrator.workflow.stages.validating import models as _models
from orchestrator.workflow.stages.validating import state as _state


def _stranded_fix_unpushed(
    spec: config.RepoSpec, wt: Path, state: PinnedState, issue: Issue
) -> bool:
    """True when a clean worktree HEAD is strictly ahead of the remote PR
    branch -- a fix an earlier parked run committed but never published.

    The shape arises when the publish was blocked at commit time (e.g. a
    dirty-worktree park whose stray files a human later had the dev clean
    up): every later resume sees `after_sha == before_sha`, so without
    this check the stranded commit can never reach the PR and the issue
    ping-pongs between `awaiting_human` parks forever.

    Conservative by construction: a dirty tree, a failed fetch, or a
    remote that moved (`behind > 0` -- pushing would race a head we have
    not reconciled) all report False so the caller falls back to the
    question park instead of pushing blind.
    """
    if _verification_probes._worktree_dirty_files(wt):
        return False
    branch = _worktree_paths._resolve_branch_name(state, spec, issue.number)
    fetch = _authentication._authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=wt,
    )
    if fetch.returncode != 0:
        return False
    ahead, behind = _publication_probes._branch_ahead_behind(spec, wt, branch)
    return ahead > 0 and behind == 0


def _park_dev_fix_timeout(
    gh: GitHubClient, issue: Issue, state: PinnedState, before_sha: str,
) -> None:
    _guards._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent timed out after {config.AGENT_TIMEOUT}s, "
        "manual intervention needed.",
        reason=_state._REASON_AGENT_TIMEOUT,
    )
    state.set(_state._PARK_REASON, _state._REASON_AGENT_TIMEOUT)
    state.set(_state._PRE_DEV_FIX_SHA, before_sha or "")


def _dev_fix_is_publishable(
    spec: config.RepoSpec, issue: Issue, state: PinnedState, run: _models._DevFixRun,
) -> bool:
    after_sha = run.after_sha
    if after_sha is None:
        after_sha = _verification_probes._head_sha(run.worktree)
    if after_sha and after_sha != run.before_sha:
        return True
    return bool(after_sha) and _stranded_fix_unpushed(
        spec, run.worktree, state, issue,
    )


def _publish_dev_fix(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run: _models._DevFixRun,
) -> bool:
    state.set("silent_park_count", 0)
    dirty = _verification_probes._worktree_dirty_files(run.worktree)
    if dirty:
        _dev_parks._on_dirty_worktree(gh, issue, state, run.agent_result, dirty)
        return False
    branch = _worktree_paths._resolve_branch_name(state, spec, issue.number)
    if _authentication._push_branch(spec, run.worktree, branch):
        return True
    _guards._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
        reason=_state._REASON_PUSH_FAILED,
    )
    state.set(_state._PARK_REASON, _state._REASON_PUSH_FAILED)
    return False


def _dispose_dev_fix_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run: _models._DevFixRun,
) -> bool:
    if run.agent_result.interrupted:
        return False
    if run.agent_result.timed_out:
        _park_dev_fix_timeout(gh, issue, state, run.before_sha)
        return False
    if not _dev_fix_is_publishable(spec, issue, state, run):
        _dev_parks._on_question(gh, issue, state, run.agent_result)
        return False
    return _publish_dev_fix(gh, spec, issue, state, run)


def _handle_dev_fix_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    *context_args,
    **fields,
) -> bool:
    """Post-agent handling for a dev fix during validating.

    Returns True if a fix was committed, pushed, and the caller should
    advance the label (validating routes the issue back to `validating`
    on True so the reviewer re-runs against the new head; any stale
    approval state must be reset by the caller before relabeling). A
    no-new-commit run also returns True when it published a stranded fix
    a prior parked run had committed (see `_stranded_fix_unpushed`).
    Returns False if the run produced no fix (timeout, no-new-commit,
    dirty tree, or push failure); caller should write state and return.
    A shutdown-killed (interrupted) run also returns False WITHOUT parking,
    posting, or publishing, so the next tick re-runs the dev cleanly.

    `after_sha`, when provided, is the post-agent HEAD the caller already
    read (e.g. the fixing handler's ACK fast path); passing it avoids a
    redundant `_head_sha` call. When None it is read here.
    """
    state, run = _models._dev_fix_run(context_args, fields)
    return _dispose_dev_fix_result(gh, spec, issue, state, run)


def _bump_review_round(state: PinnedState) -> None:
    current_round = int(state.get(_state._REVIEW_ROUND) or 0)
    state.set(_state._REVIEW_ROUND, current_round + 1)
