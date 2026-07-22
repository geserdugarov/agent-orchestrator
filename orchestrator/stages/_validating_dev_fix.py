# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating dev fix."""
from __future__ import annotations

from orchestrator.stages import _validating_state as _state
from orchestrator.stages import validating as _owner

_DevFixRun = _owner._DevFixRun
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Path = _owner.Path
PinnedState = _owner.PinnedState
config = _owner.config
_PARK_REASON = _state._PARK_REASON
_PRE_DEV_FIX_SHA = _state._PRE_DEV_FIX_SHA
_REASON_AGENT_TIMEOUT = _state._REASON_AGENT_TIMEOUT
_REASON_PUSH_FAILED = _state._REASON_PUSH_FAILED


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
    from orchestrator import workflow as _wf

    if _wf._worktree_dirty_files(wt):
        return False
    branch = _wf._resolve_branch_name(state, spec, issue.number)
    fetch = _wf._authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=wt,
    )
    if fetch.returncode != 0:
        return False
    ahead, behind = _wf._branch_ahead_behind(spec, wt, branch)
    return ahead > 0 and behind == 0


def _park_dev_fix_timeout(
    gh: GitHubClient, issue: Issue, state: PinnedState, before_sha: str,
) -> None:
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent timed out after {config.AGENT_TIMEOUT}s, "
        "manual intervention needed.",
        reason=_REASON_AGENT_TIMEOUT,
    )
    state.set(_PARK_REASON, _REASON_AGENT_TIMEOUT)
    state.set(_PRE_DEV_FIX_SHA, before_sha or "")


def _dev_fix_is_publishable(
    spec: config.RepoSpec, issue: Issue, state: PinnedState, run: _DevFixRun,
) -> bool:
    from orchestrator import workflow as _wf

    after_sha = run.after_sha
    if after_sha is None:
        after_sha = _wf._head_sha(run.worktree)
    if after_sha and after_sha != run.before_sha:
        return True
    return bool(after_sha) and _owner._stranded_fix_unpushed(
        spec, run.worktree, state, issue,
    )


def _publish_dev_fix(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run: _DevFixRun,
) -> bool:
    from orchestrator import workflow as _wf

    state.set("silent_park_count", 0)
    dirty = _wf._worktree_dirty_files(run.worktree)
    if dirty:
        _wf._on_dirty_worktree(gh, issue, state, run.agent_result, dirty)
        return False
    branch = _wf._resolve_branch_name(state, spec, issue.number)
    if _wf._push_branch(spec, run.worktree, branch):
        return True
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
        reason=_REASON_PUSH_FAILED,
    )
    state.set(_PARK_REASON, _REASON_PUSH_FAILED)
    return False


def _dispose_dev_fix_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run: _DevFixRun,
) -> bool:
    from orchestrator import workflow as _wf

    if run.agent_result.interrupted:
        return False
    if run.agent_result.timed_out:
        _owner._park_dev_fix_timeout(gh, issue, state, run.before_sha)
        return False
    if not _owner._dev_fix_is_publishable(spec, issue, state, run):
        _wf._on_question(gh, issue, state, run.agent_result)
        return False
    return _owner._publish_dev_fix(gh, spec, issue, state, run)


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
    state, run = _owner._dev_fix_run(context_args, fields)
    return _owner._dispose_dev_fix_result(gh, spec, issue, state, run)
