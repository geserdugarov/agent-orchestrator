# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition run."""
from __future__ import annotations

from contextlib import ExitStack

from orchestrator.stages._decomposition_models import _DecomposerCleanup
from orchestrator.stages import _decomposition_state as _state
from orchestrator.stages import decomposition as _owner

_DecomposerRunPlan = _owner._DecomposerRunPlan
AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
config = _owner.config
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_CHILDREN = _state._CHILDREN
_UMBRELLA = _state._UMBRELLA


def _settle_decomposer_run(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    decomposer_result: AgentResult,
) -> bool:
    """Fold this run's usage and park on a live pause or timeout.

    Returns True when the caller must return (paused or timed out), False
    to continue to the dirty-worktree check and manifest dispatch. None of
    these paths preserve the decompose worktree: the caller's `finally`
    tears it down on return. The read-only dirty/commits park (which DOES
    preserve the worktree) stays inline in `_handle_decomposing` so
    `keep_worktree` is set BEFORE the park's side effects run.
    """
    from orchestrator import workflow as _wf

    # Live pause: an operator applied `paused` / `backlog` while the
    # decomposer ran (fresh spawn or awaiting-human resume). Dispatch only
    # saw the pre-run labels, so re-check a freshly fetched issue and return
    # WITHOUT folding usage, parking on timeout, creating child issues,
    # relabeling, or writing pinned state -- durable GitHub state stays
    # exactly as the prior tick left it and the next tick re-runs the
    # decomposer once the label is removed. The read-only decompose worktree
    # is torn down by the caller's `finally` as on any normal exit and
    # recreated on the re-run.
    if _wf._paused_during_agent_run(gh, issue):
        return True

    state.set("last_agent_action_at", _wf._now_iso())
    # Fold this run's usage into the per-issue counters at the convergence
    # of the fresh-spawn and awaiting-human resume branches, so a real
    # resume exit is counted exactly once and the no-new-comment resume
    # (which returned above without running the agent) never touches the
    # counters. Interrupted runs are excluded entirely: the read-only
    # dirty/commits park below still writes pinned state (to preserve the
    # inspection worktree), so folding a killed run's usage first would
    # persist a counter the interrupted contract says must not accrue. The
    # clean-interrupted case is additionally short-circuited by the
    # `_ignore_if_interrupted` guard in `_handle_decomposing`.
    if not decomposer_result.interrupted:
        _wf._accumulate_issue_usage(state, decomposer_result.usage)

    if decomposer_result.timed_out:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} decomposer timed out after "
            f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
            reason="decomposer_timeout",
        )
        gh.write_pinned_state(issue, state)
        return True
    return False


def _dispatch_decomposer_manifest(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    decomposer_result: AgentResult,
) -> None:
    """Parse the decomposer's final message and route on the outcome.

    Parks awaiting human on an invalid / silent / question manifest,
    finalizes a `single` decision to `ready`, or creates the `split`
    children and finalizes the parent to `blocked` / `umbrella`.
    """
    from orchestrator import workflow as _wf

    last_msg = decomposer_result.last_message or ""
    parsed, error = _wf._parse_manifest(last_msg)

    if parsed is None:
        _owner._park_unparsed_manifest(
            gh, issue, state, decomposer_result, error,
        )
        return

    if parsed["decision"] == "single":
        _owner._finalize_single_decision(gh, issue, state, parsed)
        return

    # decision == "split".
    split_plan = _owner._create_child_issues(
        gh,
        issue,
        state,
        parsed[_CHILDREN],
        bool(parsed.get(_UMBRELLA)),
    )
    if split_plan is None:
        return
    _owner._finalize_split(gh, issue, state, split_plan)


def _prepare_decomposer_run(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
) -> _DecomposerRunPlan:
    # User-content drift FIRST, so it runs BEFORE the half-finished recovery:
    # otherwise recovery could finalize against a stale manifest when the issue
    # was edited during a crash window.
    _owner._reset_decomposing_on_drift(gh, issue, state)

    if _owner._recover_stale_manifest(gh, issue, state):
        return _DecomposerRunPlan(agent_result=None)

    if _owner._route_disabled_to_implementing(gh, spec, issue, state):
        return _DecomposerRunPlan(agent_result=None)

    if state.get(_AWAITING_HUMAN):
        decomposer_result = _owner._resume_decomposer_on_human_reply(
            gh, spec, issue, state,
        )
        return _DecomposerRunPlan(
            agent_result=decomposer_result,
            # A no-reply dirty park keeps its inspection worktree intact.
            keep_worktree=decomposer_result is None,
        )
    return _DecomposerRunPlan(
        agent_result=_owner._spawn_fresh_decomposer(gh, spec, issue, state),
    )


def _process_decomposer_run(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run_plan: _DecomposerRunPlan,
) -> None:
    from orchestrator import workflow as _wf

    decomposer_result = run_plan.agent_result
    if decomposer_result is None:
        return

    if _owner._settle_decomposer_run(gh, issue, state, decomposer_result):
        return

    # The decomposer is read-only. Preserve a changed worktree for operator
    # inspection, setting the cleanup policy before parking or persistence can
    # raise and trigger the handler's finally block.
    wt = _wf._decompose_worktree_path(spec, issue.number)
    if _wf._has_new_commits(spec, wt) or _wf._worktree_dirty_files(wt):
        run_plan.keep_worktree = True
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} decomposer left commits or "
            "uncommitted changes in the worktree, but it must be "
            "read-only. Reset the worktree before resuming.",
            reason="decomposer_dirty",
        )
        gh.write_pinned_state(issue, state)
        return

    # An interrupted run has no trustworthy manifest. The read-only check
    # stays first so changes left by a killed run remain available to inspect.
    if _wf._ignore_if_interrupted(issue, decomposer_result):
        return

    _owner._dispatch_decomposer_manifest(gh, issue, state, decomposer_result)


def _handle_decomposing(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)
    cleanup = _DecomposerCleanup(
        spec=spec,
        issue_number=issue.number,
        run_plan=_DecomposerRunPlan(agent_result=None),
    )
    with ExitStack() as cleanup_stack:
        cleanup_stack.callback(cleanup.close)
        cleanup.run_plan = _owner._prepare_decomposer_run(
            gh,
            spec,
            issue,
            state,
        )
        _owner._process_decomposer_run(
            gh,
            spec,
            issue,
            state,
            cleanup.run_plan,
        )
