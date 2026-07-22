# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Question session."""
from __future__ import annotations

from orchestrator.stages import _question_state as _state
from orchestrator.stages import question as _owner

AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Path = _owner.Path
PinnedState = _owner.PinnedState
config = _owner.config
dataclass = _owner.dataclass
filter_trusted = _owner.filter_trusted
_QUESTION_AGENT_KEY = _state._QUESTION_AGENT_KEY
_QUESTION_SESSION_KEY = _state._QUESTION_SESSION_KEY
_QUESTION_STAGE = _state._QUESTION_STAGE
_UNSAFE_QUESTION_PARKS = _state._UNSAFE_QUESTION_PARKS


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
