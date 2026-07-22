# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Question outcomes."""
from __future__ import annotations

from orchestrator.stages import _question_state as _state
from orchestrator.stages import question as _owner

_QuestionOutcome = _owner._QuestionOutcome
_QuestionRun = _owner._QuestionRun
AgentResult = _owner.AgentResult
config = _owner.config
_QUESTION_ANSWER = _state._QUESTION_ANSWER
_QUESTION_COMMITS = _state._QUESTION_COMMITS
_QUESTION_DIRTY = _state._QUESTION_DIRTY
_QUESTION_SILENT = _state._QUESTION_SILENT
_QUESTION_TIMEOUT = _state._QUESTION_TIMEOUT


def _assess_question_outcome(
    run: _QuestionRun, question_result: AgentResult,
) -> _QuestionOutcome:
    """Inspect a completed agent run in the stage's required order."""
    from orchestrator import workflow as _wf

    # A live pause must leave every in-memory session and watermark mutation
    # unpersisted so the next active tick can replay the same durable state.
    if _wf._paused_during_agent_run(run.gh, run.issue):
        return _QuestionOutcome(None, run.keep_worktree)

    run.state.set("last_question_at", _wf._now_iso())
    if not question_result.interrupted:
        _wf._accumulate_issue_usage(run.state, question_result.usage)

    if question_result.timed_out:
        return _QuestionOutcome(_QUESTION_TIMEOUT, True)

    return _owner._assess_question_worktree(run, question_result)


def _assess_question_worktree(
    run: _QuestionRun, question_result: AgentResult,
) -> _QuestionOutcome:
    """Classify a completed, non-timeout run from its worktree and answer.

    Read-only violations (new commits / dirty tree) take precedence over
    interruption so a killed run that changed the tree still leaves an
    inspection target for the operator.
    """
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(run.spec, run.issue.number)
    if _wf._has_new_commits(run.spec, worktree):
        return _QuestionOutcome(_QUESTION_COMMITS, True)

    dirty_files = tuple(_wf._worktree_dirty_files(worktree))
    if dirty_files:
        return _QuestionOutcome(
            _QUESTION_DIRTY, True, dirty_files=dirty_files,
        )

    if _wf._ignore_if_interrupted(run.issue, question_result):
        return _QuestionOutcome(None, run.keep_worktree)

    answer = (question_result.last_message or "").strip()
    if answer:
        return _QuestionOutcome(
            _QUESTION_ANSWER, False, answer=answer,
        )
    return _QuestionOutcome(_QUESTION_SILENT, False)


def _park_dirty_question(
    run: _QuestionRun, dirty_files: tuple[str, ...],
) -> None:
    shown_files = dirty_files[:10]
    display_lines = [f"- `{file_path}`" for file_path in shown_files]
    hidden_count = len(dirty_files) - len(shown_files)
    if hidden_count:
        display_lines.append(f"- ... ({hidden_count} more)")
    files_markdown = "\n".join(display_lines)
    _owner._park_question(
        run,
        f"{config.HITL_MENTIONS} question agent left "
        f"{len(dirty_files)} uncommitted change(s) but this stage "
        "is read-only; refusing to push. Reset the worktree "
        f"before resuming.\n\n{files_markdown}",
        reason=_QUESTION_DIRTY,
    )


def _park_silent_question(
    run: _QuestionRun, question_result: AgentResult,
) -> None:
    from orchestrator import workflow as _wf

    diagnostics = _wf._format_stderr_diagnostics(
        question_result, "Question agent",
    )
    _owner._park_question(
        run,
        f"{config.HITL_MENTIONS} question agent produced no "
        "output (likely a session-resume failure); manual "
        f"intervention needed.{diagnostics}",
        reason=_QUESTION_SILENT,
    )
    _wf.log.warning(
        "issue=#%s question agent produced no output; "
        "exit_code=%d timed_out=%s stderr_tail=%r",
        run.issue.number,
        question_result.exit_code,
        question_result.timed_out,
        _wf._stderr_log_tail(question_result),
    )


def _park_answered_question(run: _QuestionRun, answer: str) -> None:
    quoted_lines = answer.replace("\n", "\n> ")
    quoted_answer = f"> {quoted_lines}"
    _owner._park_question(
        run,
        f"{config.HITL_MENTIONS} question agent responded:\n\n"
        f"{quoted_answer}",
        reason=_QUESTION_ANSWER,
    )


def _route_question_outcome(
    run: _QuestionRun,
    question_result: AgentResult,
    outcome: _QuestionOutcome,
) -> None:
    """Persist the park selected by `_assess_question_outcome`."""
    if outcome.park_reason == _QUESTION_TIMEOUT:
        _owner._park_question(
            run,
            f"{config.HITL_MENTIONS} question agent timed out "
            f"after {config.AGENT_TIMEOUT}s; manual intervention "
            "needed. The per-issue worktree is left intact for inspection.",
            reason=_QUESTION_TIMEOUT,
        )
        return
    if outcome.park_reason == _QUESTION_COMMITS:
        _owner._park_question(
            run,
            f"{config.HITL_MENTIONS} question agent committed in "
            "the worktree but this stage is read-only; refusing "
            "to push. Reset the worktree before resuming.",
            reason=_QUESTION_COMMITS,
        )
        return
    if outcome.park_reason == _QUESTION_DIRTY:
        _owner._park_dirty_question(run, outcome.dirty_files)
        return
    if outcome.park_reason == _QUESTION_SILENT:
        _owner._park_silent_question(run, question_result)
        return
    _owner._park_answered_question(run, outcome.answer)
