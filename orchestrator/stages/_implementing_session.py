# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing session."""
from __future__ import annotations

from orchestrator.stages import _implement_state as _state
from orchestrator.stages import implementing as _owner

_DevResumePlan = _owner._DevResumePlan
_DevSession = _owner._DevSession
AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
config = _owner.config
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_CODEX_SESSION_ID = _state._CODEX_SESSION_ID
_DEV_AGENT = _state._DEV_AGENT
_DEV_RESUME_COUNT = _state._DEV_RESUME_COUNT
_DEV_SESSION_ID = _state._DEV_SESSION_ID
_IMPLEMENTING_STAGE = _state._IMPLEMENTING_STAGE
_RETRY_COUNT = _state._RETRY_COUNT
_RETRY_WINDOW_START = _state._RETRY_WINDOW_START
_SILENT_PARKS_BEFORE_FRESH_SESSION = _state._SILENT_PARKS_BEFORE_FRESH_SESSION
_SILENT_PARK_COUNT = _state._SILENT_PARK_COUNT


def _is_poisoned_session_failure(
    backend: str, agent_result: AgentResult,
) -> bool:
    """True iff resuming this session is futile and a fresh spawn is the only
    recovery: the session was GC'd (stale) or its transcript overflowed the
    model context window. Both clear the pinned session id and retry once as
    a fresh spawn in `_resume_dev_with_text`.
    """
    return (
        _owner._is_stale_session_failure(backend, agent_result)
        or _owner._is_context_overflow_failure(backend, agent_result)
    )


def _drop_poisoned_dev_session(state: PinnedState) -> None:
    """Clear the pinned dev session id (and legacy `codex_session_id`).

    Preserves the stored `dev_agent` spec when one is already pinned --
    a poisoned session is a transcript problem, not a backend-selection
    problem, so the fresh spawn that follows must replay the exact same
    backend+args. Writing the parsed backend back here would silently
    strip the configured CLI args from the spec and switch a `codex -m
    gpt-5.5 -c '...'` issue back to bare `codex` on the next resume.

    When the issue is on the legacy `codex_session_id` schema (no
    `dev_agent` ever written), pin `dev_agent="codex"` BEFORE clearing
    the legacy field. Without this, the next `_read_dev_session` would
    fall through to the config default and a `DEV_AGENT=claude` flip
    would silently switch the issue from codex to claude on retry.

    Clearing the legacy field too leaves no trace of the dropped
    session anywhere.
    """
    if not state.get(_DEV_AGENT) and state.get(_CODEX_SESSION_ID) is not None:
        state.set(_DEV_AGENT, "codex")
    state.set(_DEV_SESSION_ID, None)
    state.set(_CODEX_SESSION_ID, None)
    state.set(_SILENT_PARK_COUNT, 0)
    # The resume budget is per-session; clearing the session resets it so the
    # fresh spawn that follows starts its own count from zero.
    state.set(_DEV_RESUME_COUNT, 0)


def _check_and_increment_retry_budget(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    stage: str = _IMPLEMENTING_STAGE,
) -> bool:
    """Gate fresh agent spawns by a per-issue 24h retry cap.

    The window starts at the first counted attempt and resets once 24h after
    that start has elapsed -- a fixed window per issue, not a true rolling
    window, but enough to stop a stuck issue from burning tokens for a day.
    Implementing and decomposing share the same per-issue counter on
    purpose: both consume the issue's daily spawn budget.

    Returns True if the spawn is allowed (and the budget was incremented);
    False if the cap is exhausted (and the issue was parked on awaiting_human).

    Only fresh spawns count. Resumes on human reply and recovered-worktree
    pushes are explicit unblock signals or carry-over work, not retries.
    Caller writes pinned state after this returns; on the False branch we have
    already parked, so caller's pinned-state write commits the park.
    """
    from orchestrator import workflow as _wf
    from datetime import datetime, timedelta, timezone

    cap = config.MAX_RETRIES_PER_DAY
    if cap <= 0:
        return True

    now = datetime.now(timezone.utc)
    window_start_raw = state.get(_RETRY_WINDOW_START)
    window_start: Optional[datetime] = None
    if window_start_raw:
        try:
            window_start = datetime.fromisoformat(window_start_raw)
        except (TypeError, ValueError):
            window_start = None

    if window_start is None or now - window_start > timedelta(hours=24):
        # Window absent/corrupt/expired: open a new one.
        state.set(_RETRY_WINDOW_START, _wf._now_iso())
        state.set(_RETRY_COUNT, 0)
        window_start_raw = state.get(_RETRY_WINDOW_START)

    count = int(state.get(_RETRY_COUNT) or 0)
    if count >= cap:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} hit retry cap ({cap}/day) for "
            f"{stage}; manual intervention needed. "
            f"Window opened at {window_start_raw}.",
            reason="retry_cap",
        )
        return False

    state.set(_RETRY_COUNT, count + 1)
    return True


def _resolve_dev_session_for_resume(
    issue: Issue, state: PinnedState
) -> _DevResumePlan:
    """Read the locked dev session and decide fresh-spawn vs resume.

    Returns a plan whose session locks the agent spec, backend, arguments,
    and session id alongside the fresh-spawn decision and resume count.

    The plan's session id is cleared to None -- and `fresh_spawn` set True --
    when the session must be retired proactively: either the resume budget
    (`DEV_SESSION_MAX_RESUMES`) or the silent-park streak
    (`_SILENT_PARKS_BEFORE_FRESH_SESSION`) is exhausted. `--resume` replays
    the entire accumulated transcript every time, so a session resumed many
    times creeps toward the model context window; rotating proactively rebuilds
    a small prompt from durable state and caps that creep before it overflows.
    Retirement drops the pinned session id BEFORE the spawn (via
    `_drop_poisoned_dev_session`, which also zeroes `dev_resume_count`) so a
    fresh spawn that returns no session id does not leave the next tick reading
    the retired id and burning another retry.

    A None session id on entry (no live session to resume: the documenting
    initial pass, or a prior backend hiccup that committed but dropped
    `dev_session_id` while leaving `dev_agent` pinned) also yields
    `fresh_spawn=True`. Such a spawn opens a NEW session -- re-grounded by the
    caller and its returned id persisted -- and is NOT charged against the
    resume budget, whose checks require a non-None session id.
    """
    from orchestrator import workflow as _wf

    session = _DevSession(*_owner._read_dev_session(state))
    silent_count = int(state.get(_SILENT_PARK_COUNT) or 0)
    resume_count = int(state.get(_DEV_RESUME_COUNT) or 0)
    retirement_reason = _owner._dev_session_retirement_reason(
        session.session_id, resume_count, silent_count,
    )
    if retirement_reason is not None:
        _wf.log.info(
            "issue=#%d retiring dev session %r (%s); starting fresh",
            issue.number, session.session_id, retirement_reason,
        )
        _owner._drop_poisoned_dev_session(state)
        session = _DevSession(
            session.spec, session.backend, session.extra_args, None,
        )
    return _DevResumePlan(
        session=session,
        fresh_spawn=session.session_id is None,
        resume_count=resume_count,
    )


def _dev_session_retirement_reason(
    session_id: Optional[str], resume_count: int, silent_count: int,
) -> Optional[str]:
    if session_id is None:
        return None
    max_resumes = config.DEV_SESSION_MAX_RESUMES
    if max_resumes > 0 and resume_count >= max_resumes:
        return f"resume budget reached: {resume_count} >= {max_resumes}"
    if silent_count >= _SILENT_PARKS_BEFORE_FRESH_SESSION:
        return f"{silent_count} consecutive silent parks"
    return None


def _build_dev_spawn_prompt(
    spec: config.RepoSpec,
    issue: Issue,
    followup_text: str,
    *,
    followup_has_tracked_repos: bool,
    fresh: bool,
) -> str:
    """Prompt text for a dev resume/spawn.

    A resume already carries the issue requirements + conversation in its
    replayed transcript, so it gets the bare followup. A fresh spawn has no
    transcript, so it is re-grounded with `_build_fresh_respawn_preamble`
    (issue body + recent comments) pointed at the committed branch where the
    retired session's work survives. When the followup already embeds the
    tracked-repos block (documentation prompts), no sibling specs are passed so
    the block builder returns "" -- otherwise the composed prompt would list
    the tracked repos twice.
    """
    from orchestrator import workflow as _wf

    if not fresh:
        return followup_text
    preamble_specs = (
        [] if followup_has_tracked_repos else config.default_repo_specs()
    )
    preamble = _wf._build_fresh_respawn_preamble(
        spec, issue, _wf._recent_comments_text(issue), preamble_specs,
    )
    return f"{preamble}\n\n{followup_text}"


def _persist_dev_session_after_run(
    state: PinnedState,
    agent_result: AgentResult,
    *,
    fresh_spawn: bool,
    resume_count: int,
) -> None:
    """Record the session id / resume budget after a dev run and clear the
    awaiting-human flag (the caller reacted to a fresh human signal).

    A fresh spawn that produced a session id pins it and zeroes the resume
    budget so the new session starts its own count -- covering both rotation /
    poisoned-session recovery (which already reset the count) and the entry
    case where a stale count left by a prior session would otherwise rotate the
    brand-new session early. A resumed session is charged one against the
    budget so the next tick can rotate once the transcript has grown enough.
    """
    if fresh_spawn:
        if agent_result.session_id:
            state.set(_DEV_SESSION_ID, agent_result.session_id)
            state.set(_DEV_RESUME_COUNT, 0)
    else:
        state.set(_DEV_RESUME_COUNT, resume_count + 1)
    state.set(_AWAITING_HUMAN, False)
