# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The two shapes a question tick can take, and the park every exit lands on.

`awaiting_human` is the only thing that tells the two apart: set, the issue is
parked on an answer and the tick is looking for a human reply to feed back into
the locked session; clear, this is the conversation's first round. Both routes
end in the same spawn, which is why the worktree preparation differs only in
whether the checkout already exists -- a resume reuses the tree the prior round
inspected, and re-creates it only when a safe teardown removed it.

The spawn sits here rather than with the session it carries because retaining a
returned session id is the last thing a run does, not a disposition its caller
is trusted to make: a CLI that fails after handing one back still has to leave
the next resume pointed at the same conversation.

`_park_question` is the funnel every exit lands on, and it exists because the
shared park helper clears `park_reason`: the stage-specific reason has to be
restored after it, or the implementing relabel guard loses the `question_`
prefix it refuses on.
"""
from __future__ import annotations

from pathlib import Path

from orchestrator.agents import AgentResult
from orchestrator.git.worktrees import creation as _worktree_creation
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.stages.question import models as _models
from orchestrator.workflow.stages.question import session as _session
from orchestrator.workflow.stages.question import state as _state


def _execute_question_prompt(
    run: _models._QuestionRun,
    session: _models._QuestionSession,
    prompt: str,
    worktree: Path,
    resume_session_id: str | None = None,
) -> AgentResult:
    """Run one question prompt and retain any session id it returns."""
    question_result = _usage._run_agent_tracked(
        run.gh,
        run.issue.number,
        agent_role=_state._QUESTION_STAGE,
        stage=_state._QUESTION_STAGE,
        backend=session.backend,
        prompt=prompt,
        cwd=worktree,
        agent_spec=session.agent_spec,
        resume_session_id=resume_session_id,
        extra_args=session.extra_args,
    )
    if question_result.session_id:
        run.state.set(_state._QUESTION_SESSION_KEY, question_result.session_id)
    return question_result


def _resume_question_on_human_reply(
    run: _models._QuestionRun,
) -> AgentResult | None:
    """Resume the question session with new issue-thread comments.

    Returns the AgentResult, or None if no new comments arrived since
    the last park (caller should return without writing state).
    """
    new_comments = _session._consume_new_human_replies(
        run.gh, run.issue, run.state,
    )
    if new_comments is None:
        return None
    worktree = _worktree_paths._worktree_path(run.spec, run.issue.number)
    if not worktree.exists():
        worktree = _worktree_creation._ensure_worktree(
            run.spec,
            run.issue.number,
            branch=_worktree_paths._resolve_branch_name(
                run.state, run.spec, run.issue.number,
            ),
        )
    session = _session._read_question_session(run.state)
    prompt = _session._build_question_resume_prompt(
        run.spec, run.issue, new_comments, session.session_id,
    )
    question_result = _execute_question_prompt(
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


def _spawn_fresh_question(run: _models._QuestionRun) -> AgentResult:
    """Create a clean worktree and execute the initial question prompt."""
    worktree = _worktree_creation._ensure_worktree(
        run.spec,
        run.issue.number,
        branch=_worktree_paths._resolve_branch_name(
            run.state, run.spec, run.issue.number,
        ),
    )
    session = _session._read_question_session(run.state)
    # Persist the full spec before the spawn so a run that returns no session
    # id still locks future replies to the backend and args that actually ran.
    run.state.set(_state._QUESTION_AGENT_KEY, session.agent_spec)
    prompt = _session._build_first_round_question_prompt(run.spec, run.issue)
    return _execute_question_prompt(run, session, prompt, worktree)


def _select_question_run(
    run: _models._QuestionRun,
) -> AgentResult | None:
    """Resume a parked conversation or start its first agent run."""
    if run.state.get("awaiting_human"):
        return _resume_question_on_human_reply(run)
    return _spawn_fresh_question(run)


def _park_question(
    run: _models._QuestionRun,
    message: str,
    *,
    reason: str,
) -> None:
    """Park the issue awaiting human and emit the `park_awaiting_human`
    audit event with the question-stage reason tag.

    The shared park helper clears `park_reason`, so this funnel restores the
    stage-specific reason and persists the completed state mutation.
    """
    _guards._park_awaiting_human(
        run.gh, run.issue, run.state, message, reason=reason,
    )
    run.state.set("park_reason", reason)
    run.gh.write_pinned_state(run.issue, run.state)
