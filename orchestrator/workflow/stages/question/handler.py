# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""One `question` tick, in the order its questions have to be asked.

The closed-issue finalize is read first and outranks everything below it: a
human closing the thread is the answer, so there is nothing left for an agent to
say and the tick reaches that conclusion without spending a run.

Everything else runs inside the teardown context, and that context is why both
worktree teardowns live on this owner. The question stage never pushes, so the
per-issue checkout is scratch space that has to disappear on every safe exit --
including the ones that raise, which is what wrapping the run in a context
manager buys. Setting `keep_worktree` from the assessment BEFORE the park is
published is the other half: an unsafe outcome's tree survives even when posting
its comment fails. The order is load-bearing in both directions -- tearing down
too eagerly destroys the evidence an operator was parked to inspect, and tearing
down too late leaves a stale checkout that a later `question` -> `implementing`
relabel would restore.
"""
from __future__ import annotations

import contextlib

from github.Issue import Issue

from orchestrator import config
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.git.worktrees import terminal as _worktree_terminal
from orchestrator.github.client import GitHubClient
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.stages.question import models as _models
from orchestrator.workflow.stages.question import outcomes as _outcomes
from orchestrator.workflow.stages.question import run as _run
from orchestrator.workflow.state import WorkflowLabel


def _teardown_question_worktree(run: _models._QuestionRun) -> None:
    """Remove the scratch checkout and the branch it sits on.

    The branch name is resolved rather than derived so a worktree created under
    the legacy naming scheme is still the one torn down.
    """
    _worktree_terminal._cleanup_question_worktree(
        run.spec,
        run.issue.number,
        branch=_worktree_paths._resolve_branch_name(
            run.state, run.spec, run.issue.number,
        ),
    )


def _finalize_closed_question(run: _models._QuestionRun) -> bool:
    """Finalize a manually closed Q&A thread without spawning an agent."""
    if getattr(run.issue, "state", "open") != "closed":
        return False
    run.state.set("question_closed_at", _usage._now_iso())
    run.gh.set_workflow_label(run.issue, WorkflowLabel.DONE)
    # The receipt is posted before the single state write so its comment id is
    # tracked alongside the terminal timestamp.
    _usage._post_issue_usage_verdict(run.gh, run.issue, run.state)
    run.gh.write_pinned_state(run.issue, run.state)
    _teardown_question_worktree(run)
    return True


def _process_question_run(run: _models._QuestionRun) -> None:
    question_result = _run._select_question_run(run)
    if question_result is None:
        return
    outcome = _outcomes._assess_question_outcome(run, question_result)
    # Set the cleanup policy before any park side effect can fail. Unsafe
    # outcomes must preserve the worktree even when posting the park raises.
    run.keep_worktree = outcome.keep_worktree
    if outcome.park_reason is not None:
        _outcomes._route_question_outcome(run, question_result, outcome)


def _cleanup_question_run(run: _models._QuestionRun) -> None:
    if run.keep_worktree:
        return
    _teardown_question_worktree(run)


@contextlib.contextmanager
def _question_run_cleanup(run: _models._QuestionRun):
    """Tear down the question worktree once the run finishes, even on error
    (unless the run marked its tree keep-on-inspection)."""
    try:
        yield
    finally:
        _cleanup_question_run(run)


def _handle_question(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue,
) -> None:
    run = _models._QuestionRun.start(gh, spec, issue)
    if _finalize_closed_question(run):
        return
    with _question_run_cleanup(run):
        _process_question_run(run)
