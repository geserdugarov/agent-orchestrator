# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Documenting handler."""
from __future__ import annotations

from orchestrator.stages import documenting as _owner

_DocumentingContext = _owner._DocumentingContext
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
config = _owner.config


def _drive_documenting_pass(ctx: _DocumentingContext):
    """Prepare the worktree, run the docs pass, and return the run outcome.

    Returns a `_DocumentingRun` ready for disposition, or None when the tick
    is already fully handled and the caller must return without disposition:
    a fetch / diverged-branch park, an awaiting-human resume with no new
    comment, a shutdown-sweep interruption, or an operator pause.
    """
    from orchestrator import workflow as _wf

    wt = _wf._ensure_pr_worktree(ctx.spec, ctx.issue.number, branch=ctx.branch)

    ahead = _owner._prepare_documenting_worktree(ctx, wt)
    if ahead is None:
        return None

    run = _owner._run_documenting_dev(ctx, wt, ahead)
    if run is None:
        return None

    ctx.state.set("last_agent_action_at", _wf._now_iso())

    # Shutdown-sweep interruption: a docs run the orchestrator killed
    # mid-flight has no trustworthy result (the recovered `ahead > 0` shape
    # synthesizes its own non-interrupted result, so only a real resume /
    # fresh-docs spawn can land here). Ignore it and return WITHOUT writing
    # pinned state -- the pre-spawn `docs_checked_sha` / watermark mutations
    # are discarded so the next process re-runs the docs pass.
    if _wf._ignore_if_interrupted(ctx.issue, run.agent_result):
        return None

    # Live pause applied while the docs agent ran: honor the decision the
    # resume helper already made (the recovered `ahead > 0` shape ran no agent
    # and reports False). Stop before the disposition posts a PR comment,
    # pushes, advances to `in_review`, or writes pinned state. The committed
    # docs work stays on the branch and republishes through the
    # recovered-worktree path once the label is removed.
    if run.paused:
        return None

    return run


def _handle_documenting(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if _owner._documenting_preconditions_handled(gh, spec, issue, state, pr_number):
        return

    ctx = _DocumentingContext(
        gh, spec, issue, state,
        _wf._resolve_branch_name(state, spec, issue.number), pr_number,
    )

    if _owner._reconcile_documenting_drift(ctx):
        return

    if _owner._documenting_parked_no_input(gh, issue, state):
        return

    run = _owner._drive_documenting_pass(ctx)
    if run is None:
        return

    _owner._dispose_documenting_outcome(ctx, run)
