# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Question run."""
from __future__ import annotations

from orchestrator.stages import _question_state as _state
from orchestrator.stages import question as _owner

_QuestionRun = _owner._QuestionRun
AgentResult = _owner.AgentResult
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
_QUESTION_AGENT_KEY = _state._QUESTION_AGENT_KEY


def _resume_question_on_human_reply(
    run: _QuestionRun,
) -> AgentResult | None:
    """Resume the question session with new issue-thread comments.

    Returns the AgentResult, or None if no new comments arrived since
    the last park (caller should return without writing state).
    """
    from orchestrator import workflow as _wf

    new_comments = _owner._consume_new_human_replies(
        run.gh, run.issue, run.state,
    )
    if new_comments is None:
        return None
    worktree = _wf._worktree_path(run.spec, run.issue.number)
    if not worktree.exists():
        worktree = _wf._ensure_worktree(
            run.spec,
            run.issue.number,
            branch=_wf._resolve_branch_name(
                run.state, run.spec, run.issue.number,
            ),
        )
    session = _owner._read_question_session(run.state)
    prompt = _owner._build_question_resume_prompt(
        run.spec, run.issue, new_comments, session.session_id,
    )
    question_result = _owner._execute_question_prompt(
        run,
        session,
        prompt,
        worktree,
        session.session_id,
    )
    # Result routing will establish the next park; until then this consumed
    # reply is no longer waiting on a human response.
    run.state.set("awaiting_human", False)
    return question_result


def _spawn_fresh_question(run: _QuestionRun) -> AgentResult:
    """Create a clean worktree and execute the initial question prompt."""
    from orchestrator import workflow as _wf

    worktree = _wf._ensure_worktree(
        run.spec,
        run.issue.number,
        branch=_wf._resolve_branch_name(
            run.state, run.spec, run.issue.number,
        ),
    )
    session = _owner._read_question_session(run.state)
    # Persist the full spec before the spawn so a run that returns no session
    # id still locks future replies to the backend and args that actually ran.
    run.state.set(_QUESTION_AGENT_KEY, session.agent_spec)
    prompt = _wf._build_question_prompt(
        run.spec,
        run.issue,
        _wf._recent_comments_text(run.issue),
        config.default_repo_specs(),
    )
    return _owner._execute_question_prompt(run, session, prompt, worktree)


def _select_question_run(run: _QuestionRun) -> AgentResult | None:
    """Resume a parked conversation or start its first agent run."""
    if run.state.get("awaiting_human"):
        return _owner._resume_question_on_human_reply(run)
    return _owner._spawn_fresh_question(run)


def _park_question(
    run: _QuestionRun,
    message: str,
    *,
    reason: str,
) -> None:
    """Park the issue awaiting human and emit the `park_awaiting_human`
    audit event with the question-stage reason tag.

    The shared park helper clears `park_reason`, so this funnel restores the
    stage-specific reason and persists the completed state mutation.
    """
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        run.gh, run.issue, run.state, message, reason=reason,
    )
    run.state.set("park_reason", reason)
    run.gh.write_pinned_state(run.issue, run.state)


def _finalize_closed_question(run: _QuestionRun) -> bool:
    """Finalize a manually closed Q&A thread without spawning an agent."""
    from orchestrator import workflow as _wf

    if getattr(run.issue, "state", "open") != "closed":
        return False
    run.state.set("question_closed_at", _wf._now_iso())
    run.gh.set_workflow_label(run.issue, WorkflowLabel.DONE)
    # The receipt is posted before the single state write so its comment id is
    # tracked alongside the terminal timestamp.
    _wf._post_issue_usage_verdict(run.gh, run.issue, run.state)
    run.gh.write_pinned_state(run.issue, run.state)
    _wf._cleanup_question_worktree(
        run.spec,
        run.issue.number,
        branch=_wf._resolve_branch_name(
            run.state, run.spec, run.issue.number,
        ),
    )
    return True
