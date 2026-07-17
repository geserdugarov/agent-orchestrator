# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Question stage handler.

Drives the `question` workflow label: an operator (or another stage)
applies it when an issue has an outstanding question the orchestrator
should attempt to answer without producing code. The handler spawns
the configured `DECOMPOSE_AGENT` in the issue's normal per-issue
worktree (`issue-N`) with a read-only question-answer prompt, posts
the agent's answer (or its own clarifying follow-up) as an issue
comment that pings `HITL_MENTIONS`, and parks awaiting human so the
human can either close the issue, relabel it, or reply to continue
the conversation.

Crash / recovery contract:

* The agent must be read-only. A run that commits or leaves uncommitted
  changes is treated as misbehavior and parked (`question_dirty` /
  `question_commits`) with the worktree left intact so the operator
  can inspect what the agent did.
* A timeout parks with `question_timeout`; a fully silent run (no
  `last_message`, non-zero exit) parks with `question_silent`.
* The locked-backend pattern from the other stages applies: the spec
  is persisted BEFORE the first spawn so a backend hiccup that yields
  no session id cannot orphan the role identity, and resumes on human
  reply re-parse the stored spec.

Open `question` issues touch only their own pinned state, so the
label is deliberately NOT in `workflow._FAMILY_AWARE_LABELS` and
fan-out concurrency is preserved.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from github.Issue import Issue

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.comment_trust import filter_trusted
from orchestrator.github import GitHubClient, PinnedState
from orchestrator.state_machine import WorkflowLabel


_QUESTION_STAGE = "question"
_QUESTION_AGENT_KEY = "question_agent"
_QUESTION_SESSION_KEY = "question_session_id"
_QUESTION_ANSWER = "question_answer"
_QUESTION_COMMITS = "question_commits"
_QUESTION_DIRTY = "question_dirty"
_QUESTION_SILENT = "question_silent"
_QUESTION_TIMEOUT = "question_timeout"


# Park reasons whose underlying condition keeps the per-issue
# worktree on disk for human inspection. `_QuestionRun.start` seeds
# the cleanup policy from this set so a no-reply tick does not tear
# down the inspection target.
#
#   `question_timeout` -- agent killed mid-run; may have committed
#                          or dirtied the tree before timeout.
#   `question_commits` -- agent committed (read-only violation).
#   `question_dirty`   -- agent left uncommitted edits (read-only
#                          violation).
#
# The safe set (cleaned at end-of-tick) is the complement:
# `question_answer`, `question_silent`, `question_unsafe_relabel`
# (set by the implementing handler when refusing the relabel; the
# worktree state is already the operator's responsibility there),
# or `None` (no prior park).
_UNSAFE_QUESTION_PARKS = frozenset((
    _QUESTION_TIMEOUT, _QUESTION_COMMITS, _QUESTION_DIRTY,
))


@dataclass
class _QuestionRun:
    """Mutable cleanup policy and stable inputs for one question-stage tick."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    keep_worktree: bool

    @classmethod
    def start(
        cls, gh: GitHubClient, spec: config.RepoSpec, issue: Issue,
    ) -> _QuestionRun:
        state = gh.read_pinned_state(issue)
        return cls(
            gh=gh,
            spec=spec,
            issue=issue,
            state=state,
            keep_worktree=state.get("park_reason") in _UNSAFE_QUESTION_PARKS,
        )


@dataclass(frozen=True)
class _QuestionSession:
    """Locked agent identity used by one question-agent invocation."""

    agent_spec: str
    backend: str
    extra_args: tuple[str, ...]
    session_id: str | None


@dataclass(frozen=True)
class _QuestionOutcome:
    """Post-agent route and the cleanup policy it requires."""

    park_reason: str | None
    keep_worktree: bool
    answer: str = ""
    dirty_files: tuple[str, ...] = ()


def _read_question_session(
    state: PinnedState,
) -> _QuestionSession:
    """Return the locked question-agent identity for an issue.

    Mirrors `_read_dev_session` / `_read_decomposer_session`: `spec` is
    the full configured command string the next run will use. Callers
    persist it verbatim BEFORE invoking `run_agent` so a fresh spawn
    that yields no `session_id` (CLI hiccup, empty `-o` file) still
    records the role identity and a later `DECOMPOSE_AGENT` env flip
    cannot retarget the next awaiting-human resume at a different
    backend.

    Legacy bare-backend values (`"codex"` / `"claude"`) round-trip
    cleanly to `(backend, ())`. When the issue has never spawned a
    question agent, the returned fields carry the current decomposer spec,
    backend, args, and an empty session id.
    """
    stored = state.get(_QUESTION_AGENT_KEY)
    if stored:
        spec = str(stored)
        backend, args = config._parse_agent_spec(_QUESTION_AGENT_KEY, spec)
        session_id = state.get(_QUESTION_SESSION_KEY)
        return _QuestionSession(
            agent_spec=spec,
            backend=backend,
            extra_args=args,
            session_id=(
                None if session_id is None else str(session_id)
            ),
        )
    return _QuestionSession(
        agent_spec=config.DECOMPOSE_AGENT_SPEC,
        backend=config.DECOMPOSE_AGENT,
        extra_args=config.DECOMPOSE_AGENT_ARGS,
        session_id=None,
    )


def _consume_new_human_replies(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> list | None:
    """Return new issue-thread comments since the last park, advancing the
    consume watermark past them.

    Returns None when nothing new arrived (caller returns without writing
    state). Mirrors `_resume_developer_on_human_reply`: the watermark advances
    BEFORE the spawn so a crashed / timed-out resume still records the comments
    as consumed (the agent did see them via the followup prompt).

    Untrusted authors are dropped up front: the live resume path feeds these
    comments straight into `_build_question_followup_prompt`, so with
    `ALLOWED_ISSUE_AUTHORS` set an outsider's reply must not steer the question
    agent NOR advance the consumed watermark. Only trusted comments are
    consumed, so an outsider reply trailing a trusted one is left unconsumed
    rather than persisted as the watermark; an all-untrusted batch leaves
    nothing to act on and is treated as "no new reply".
    """
    last_action_id = state.get("last_action_comment_id")
    new_comments = filter_trusted(gh.comments_after(issue, last_action_id))
    if not new_comments:
        return None
    state.set(
        "last_action_comment_id",
        max(reply_comment.id for reply_comment in new_comments),
    )
    return new_comments


def _build_question_resume_prompt(
    spec: config.RepoSpec,
    issue: Issue,
    new_comments: list,
    question_session_id: str | None,
) -> str:
    """Assemble the resume prompt for a human reply.

    When we have a live session to resume, the brief follow-up prompt is
    enough -- the agent already has the issue body / title / prior
    conversation cached in its session state. Without a session id (the prior
    tick's CLI hiccup left `question_session_id` empty), `_run_agent_tracked`
    starts a fresh agent that has no cached context, so a followup-only prompt
    would arrive without an issue body, title, or prior conversation and the
    agent would have nothing to answer against. Switch to the full question
    prompt in that case so the recovery spawn sees the same context a
    first-tick run would, with the human's reply visible in the conversation
    block via `_recent_comments_text`.
    """
    from orchestrator import workflow as _wf

    if question_session_id is None:
        return _wf._build_question_prompt(
            spec, issue, _wf._recent_comments_text(issue),
            config.default_repo_specs(),
        )
    return _wf._build_question_followup_prompt(new_comments)


def _execute_question_prompt(
    run: _QuestionRun,
    session: _QuestionSession,
    prompt: str,
    worktree: Path,
    resume_session_id: str | None = None,
) -> AgentResult:
    """Run one question prompt and retain any session id it returns."""
    from orchestrator import workflow as _wf

    question_result = _wf._run_agent_tracked(
        run.gh,
        run.issue.number,
        agent_role=_QUESTION_STAGE,
        stage=_QUESTION_STAGE,
        backend=session.backend,
        prompt=prompt,
        cwd=worktree,
        agent_spec=session.agent_spec,
        resume_session_id=resume_session_id,
        extra_args=session.extra_args,
    )
    if question_result.session_id:
        run.state.set(_QUESTION_SESSION_KEY, question_result.session_id)
    return question_result


def _resume_question_on_human_reply(
    run: _QuestionRun,
) -> AgentResult | None:
    """Resume the question session with new issue-thread comments.

    Returns the AgentResult, or None if no new comments arrived since
    the last park (caller should return without writing state).
    """
    from orchestrator import workflow as _wf

    new_comments = _consume_new_human_replies(
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
    session = _read_question_session(run.state)
    prompt = _build_question_resume_prompt(
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
    session = _read_question_session(run.state)
    # Persist the full spec before the spawn so a run that returns no session
    # id still locks future replies to the backend and args that actually ran.
    run.state.set(_QUESTION_AGENT_KEY, session.agent_spec)
    prompt = _wf._build_question_prompt(
        run.spec,
        run.issue,
        _wf._recent_comments_text(run.issue),
        config.default_repo_specs(),
    )
    return _execute_question_prompt(run, session, prompt, worktree)


def _select_question_run(run: _QuestionRun) -> AgentResult | None:
    """Resume a parked conversation or start its first agent run."""
    if run.state.get("awaiting_human"):
        return _resume_question_on_human_reply(run)
    return _spawn_fresh_question(run)


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

    return _assess_question_worktree(run, question_result)


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
    _park_question(
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
    _park_question(
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
    _park_question(
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
        _park_question(
            run,
            f"{config.HITL_MENTIONS} question agent timed out "
            f"after {config.AGENT_TIMEOUT}s; manual intervention "
            "needed. The per-issue worktree is left intact for inspection.",
            reason=_QUESTION_TIMEOUT,
        )
        return
    if outcome.park_reason == _QUESTION_COMMITS:
        _park_question(
            run,
            f"{config.HITL_MENTIONS} question agent committed in "
            "the worktree but this stage is read-only; refusing "
            "to push. Reset the worktree before resuming.",
            reason=_QUESTION_COMMITS,
        )
        return
    if outcome.park_reason == _QUESTION_DIRTY:
        _park_dirty_question(run, outcome.dirty_files)
        return
    if outcome.park_reason == _QUESTION_SILENT:
        _park_silent_question(run, question_result)
        return
    _park_answered_question(run, outcome.answer)


def _process_question_run(run: _QuestionRun) -> None:
    question_result = _select_question_run(run)
    if question_result is None:
        return
    outcome = _assess_question_outcome(run, question_result)
    # Set the cleanup policy before any park side effect can fail. Unsafe
    # outcomes must preserve the worktree even when posting the park raises.
    run.keep_worktree = outcome.keep_worktree
    if outcome.park_reason is not None:
        _route_question_outcome(run, question_result, outcome)


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
        _cleanup_question_run(run)


def _handle_question(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue,
) -> None:
    run = _QuestionRun.start(gh, spec, issue)
    if _finalize_closed_question(run):
        return
    with _question_run_cleanup(run):
        _process_question_run(run)
