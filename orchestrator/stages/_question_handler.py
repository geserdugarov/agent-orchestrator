# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Question handler."""
from __future__ import annotations

from orchestrator.stages import question as _owner

_QuestionRun = _owner._QuestionRun
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
config = _owner.config
contextlib = _owner.contextlib


def _process_question_run(run: _QuestionRun) -> None:
    question_result = _owner._select_question_run(run)
    if question_result is None:
        return
    outcome = _owner._assess_question_outcome(run, question_result)
    # Set the cleanup policy before any park side effect can fail. Unsafe
    # outcomes must preserve the worktree even when posting the park raises.
    run.keep_worktree = outcome.keep_worktree
    if outcome.park_reason is not None:
        _owner._route_question_outcome(run, question_result, outcome)


def _cleanup_question_run(run: _QuestionRun) -> None:
    if run.keep_worktree:
        return
    from orchestrator import workflow as _wf

    _wf._cleanup_question_worktree(
        run.spec,
        run.issue.number,
        branch=_wf._resolve_branch_name(
            run.state, run.spec, run.issue.number,
        ),
    )


@contextlib.contextmanager
def _question_run_cleanup(run: _QuestionRun):
    """Tear down the question worktree once the run finishes, even on error
    (unless the run marked its tree keep-on-inspection)."""
    try:
        yield
    finally:
        _owner._cleanup_question_run(run)


def _handle_question(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue,
) -> None:
    run = _QuestionRun.start(gh, spec, issue)
    if _owner._finalize_closed_question(run):
        return
    with _owner._question_run_cleanup(run):
        _owner._process_question_run(run)
