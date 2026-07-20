# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing stage handlers and developer-session lifecycle.

Owns `_handle_implementing` plus the developer-side primitives the rest
of the workflow re-uses: per-issue dev session lookup, resume on human
reply, poisoned-session recovery, stale-session detection, the 24h
retry budget, the post-agent disposition helpers (`_on_commits`,
`_on_question`, `_on_dirty_worktree`), the next-tick agent-timeout
recovery (`_try_recover_implementing_timeout_park`) that publishes a clean
commit a descendant finished around an implementer timeout, and the parked
`/orchestrator continue` operator command (`_handle_parked_continue_command`
/ `_retry_parked_dev_session`) that retries a session-failure park before
the drift path instead of letting the bare command read as requirement
drift. The command parser + classifier are shared via `workflow_messages`.

ALL workflow-owned helpers (`_park_awaiting_human`, `_run_agent_tracked`,
`_now_iso`, the worktree plumbing, the drift / manifest / messaging
helpers re-exported into `workflow`) are reached through the parent
module via `from orchestrator import workflow as _wf` at call time. The
compatibility surface tests rely on -- `patch.object(workflow, "_foo")`
-- has to keep working from inside the stage module too, so the
handlers must NOT direct-import these names from `workflow_drift` /
`workflow_messages` / `worktrees`; doing so would bind a stable
reference that test patches against `workflow.X` could not affect.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from github.Issue import Issue

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.comment_trust import filter_trusted
from orchestrator.state_machine import WorkflowLabel
from orchestrator.github import (
    GitHubClient,
    PinnedState,
)


# After this many consecutive `agent_silent` parks on the same
# `dev_session_id`, `_resume_dev_with_text` drops the session id and starts
# a fresh spawn. Two strikes (rather than one) tolerates a transient
# single-call blip while still preventing the resume loop from burning every
# fresh-spawn retry slot on a poisoned session that's not coming back.
_SILENT_PARKS_BEFORE_FRESH_SESSION = 2

# Substrings Claude's CLI prints to stderr when `--resume <sid>` references a
# session that no longer exists (transcript GC'd, a different host, a
# mid-stream kill, etc.). This is a deterministic, recoverable failure --
# unlike a transient API blip -- so `_resume_dev_with_text` retries once
# immediately with a cleared session id instead of waiting for the silent-
# park counter to climb to `_SILENT_PARKS_BEFORE_FRESH_SESSION`.
#
# Kept as a tuple of lowercase substrings so phrasing tweaks across Claude
# CLI releases ("No conversation found ..." / "No conversation with ID ..."
# / "Conversation ... not found") still match.
_CLAUDE_STALE_SESSION_STDERR_MARKERS: Tuple[str, ...] = (
    "no conversation found with session id",
    "no conversation found with id",
    "no conversation with session id",
    "conversation not found",
)

# Substrings Claude's CLI emits when the accumulated session transcript --
# replayed in full by `--resume <sid>` -- has outgrown the model context
# window, so the resume is rejected before any work is done. Like a stale
# session this is deterministic and unrecoverable on the SAME session: every
# subsequent resume only appends to an already-over-budget transcript and
# re-fails identically (this is why a human "continue" / "decompose and
# continue" reply never breaks the loop). Recovery is identical to the stale-
# session case -- drop the session id and retry once as a fresh spawn, which
# rebuilds a small prompt from the issue body + recent comments.
#
# The overflow phrase can carry a token-count suffix ("prompt is too long:
# 215000 tokens > 200000 maximum"), so it is matched as a PREFIX of the last
# agent message (not a substring) to avoid misclassifying an agent that
# merely quotes the phrase mid-answer, and as a substring of stderr where the
# CLI may print the same diagnostic without emitting a result event.
_CLAUDE_CONTEXT_OVERFLOW_MARKERS: Tuple[str, ...] = (
    "prompt is too long",
    "input is too long",
    "input length and `max_tokens` exceed context limit",
)

# Substrings Claude's CLI emits as its FINAL result message when the account's
# rolling session / usage quota is exhausted -- e.g. "You've hit your session
# limit · resets 7pm (Asia/Novosibirsk)". Unlike a stale session or a context
# overflow this is NOT a poisoned transcript: the session is healthy and the
# only recovery is to wait for the quota to reset and retry, which is exactly
# what an operator's `/orchestrator continue` after the reset drives. So it is
# parked as a RETRYABLE session-failure (`agent_silent`), not misread as a real
# agent question that would demand human guidance before it can resume.
#
# Matched as a PREFIX of the stripped, lowercased last agent message (the quota
# notice is the whole message, never a mid-answer aside) so a dev reply that
# merely mentions a "session limit" while discussing code is not misclassified.
# The apostrophe in "You've" is normalized to a straight `'` before matching so
# a curly rendering still hits. The empty-`last_message` case is already parked
# `agent_silent` by `_on_question`'s silent-failure branch, so only the
# non-empty message is inspected here.
_CLAUDE_SESSION_LIMIT_MESSAGE_MARKERS: Tuple[str, ...] = (
    "you've hit your session limit",
    "you've hit your usage limit",
    "you've reached your session limit",
    "you've reached your usage limit",
    "claude usage limit reached",
    "claude ai usage limit reached",
)


@dataclass(frozen=True)
class _PreparedDevRun:
    agent_result: AgentResult
    before_sha: Optional[str]
    paused: bool
    worktree: Path


@dataclass(frozen=True)
class _AgentWork:
    agent_result: AgentResult
    worktree: Path


@dataclass(frozen=True)
class _PRWork(_AgentWork):
    branch: str


@dataclass(frozen=True)
class _DevSession:
    spec: str
    backend: str
    extra_args: tuple[str, ...]
    session_id: Optional[str]


@dataclass(frozen=True)
class _DevResumePlan:
    session: _DevSession
    fresh_spawn: bool
    resume_count: int


@dataclass(frozen=True)
class _DevResumeOptions:
    followup_has_tracked_repos: bool = False
    pause_guard: bool = False

    @classmethod
    def from_fields(cls, fields: dict) -> _DevResumeOptions:
        unknown = set(fields) - {"followup_has_tracked_repos", "pause_guard"}
        if unknown:
            raise TypeError(f"unexpected resume option(s): {sorted(unknown)!r}")
        return cls(**fields)


# Pinned-state keys and park-reason / stage-name values this handler reads.
_DEV_AGENT = "dev_agent"
_DEV_SESSION_ID = "dev_session_id"
_CODEX_SESSION_ID = "codex_session_id"
_SILENT_PARK_COUNT = "silent_park_count"
_DEV_RESUME_COUNT = "dev_resume_count"
_RETRY_WINDOW_START = "retry_window_start"
_RETRY_COUNT = "retry_count"
_AWAITING_HUMAN = "awaiting_human"
_LAST_ACTION_COMMENT_ID = "last_action_comment_id"
_AGENT_TIMEOUT = "agent_timeout"
_PARK_REASON = "park_reason"
_PRE_IMPLEMENT_SHA = "pre_implement_sha"
_BRANCH = "branch"
_IMPLEMENTING_STAGE = "implementing"
_REASON_STUCK = "stuck"


def _as_blockquote(text: str) -> str:
    """Render `text` as a Markdown blockquote (each line prefixed with `> `)."""
    prefixed = text.replace("\n", "\n> ")
    return f"> {prefixed}"


def _stored_dev_session(state: PinnedState, stored) -> tuple:
    stored_spec = str(stored)
    backend, args = config._parse_agent_spec(_DEV_AGENT, stored_spec)
    session_id = state.get(_DEV_SESSION_ID)
    return (
        stored_spec,
        backend,
        args,
        None if session_id is None else str(session_id),
    )


def _read_dev_session(
    state: PinnedState,
) -> Tuple[str, str, tuple[str, ...], Optional[str]]:
    """Return (spec, backend, extra_args, dev_session_id) for an issue.

    `spec` is the full configured agent command string the next run
    will use -- callers persist it verbatim BEFORE invoking `run_agent`
    so the recorded role identity survives a spawn that returns no
    session id (CLI hiccup, missing output file, etc.). Without that,
    a fresh spawn that nevertheless commits would leave `dev_agent`
    unset and a later `DEV_AGENT` flip would silently retarget the next
    resume at a backend that never ran on this issue.

    The pinned `dev_agent` field stores that spec -- e.g. `"codex"`,
    `"claude"`, or `"codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"`
    -- as the durable role identity. Re-parsing it here means in-flight
    resumes use the same backend AND args the fresh spawn used, even
    after a `DEV_AGENT` env flip between ticks.

    Backward compatibility:
      * Legacy bare-backend values (`"codex"` / `"claude"`) re-parse to
        `(backend, ())` -- no args -- which is what those deployments
        had at the time they were spawned. `spec` is the same bare
        string; persisting it again is a no-op rewrite.
      * Legacy `codex_session_id` (written before `dev_agent` existed)
        yields `spec="codex"`. A config flip to claude cannot strand
        that session -- it stays on codex with no args.
      * When the issue has never been spawned, returns the current
        config's `(DEV_AGENT_SPEC, DEV_AGENT, DEV_AGENT_ARGS, None)`
        for the imminent fresh spawn to use AND persist.
    """
    stored = state.get(_DEV_AGENT)
    if stored:
        return _stored_dev_session(state, stored)
    legacy = state.get(_CODEX_SESSION_ID)
    if legacy is not None:
        return "codex", "codex", (), str(legacy)
    return (
        config.DEV_AGENT_SPEC,
        config.DEV_AGENT,
        config.DEV_AGENT_ARGS,
        None,
    )


def _is_stale_session_failure(
    backend: str, agent_result: AgentResult,
) -> bool:
    """True iff `agent_result` is a deterministic stale-session failure.

    Only claude is matched today: codex's resume CLI does not expose a
    comparable stable stderr marker, so codex still relies on the silent-
    park-count fallback. If/when codex grows one, add it here.
    """
    if backend != "claude":
        return False
    stderr = (agent_result.stderr or "").lower()
    if not stderr:
        return False
    return any(marker in stderr for marker in _CLAUDE_STALE_SESSION_STDERR_MARKERS)


def _is_context_overflow_failure(
    backend: str, agent_result: AgentResult,
) -> bool:
    """True iff `agent_result` is a Claude context-overflow resume failure.

    Only claude is matched today: codex's resume CLI does not expose a
    comparable stable marker. The marker is checked as a PREFIX of the
    stripped, lowercased last agent message -- so an agent that merely
    mentions the phrase mid-answer is not misclassified -- and as a substring
    of stderr, where the CLI may print the same diagnostic when it produces
    no result event at all.
    """
    if backend != "claude":
        return False
    msg = (agent_result.last_message or "").strip().lower()
    if any(msg.startswith(marker) for marker in _CLAUDE_CONTEXT_OVERFLOW_MARKERS):
        return True
    stderr = (agent_result.stderr or "").lower()
    return any(marker in stderr for marker in _CLAUDE_CONTEXT_OVERFLOW_MARKERS)


def _is_session_limit_message(agent_result: AgentResult) -> bool:
    """True iff the result message is a Claude session/usage-quota notice.

    A non-empty quota notice ("You've hit your session limit ...") is not a
    real agent question: the session is healthy and the only recovery is to
    wait for the reset and retry. Matched as a PREFIX of the normalized last
    agent message so a dev reply that merely mentions a session limit
    mid-answer is not caught. Backend-agnostic on purpose -- the phrasings are
    distinctive enough that a non-Claude backend echoing them would still be a
    quota stop, and `_on_question` (the sole caller) has no backend in hand.
    """
    msg = (agent_result.last_message or "").strip().lower().replace("’", "'")
    return any(
        msg.startswith(marker) for marker in _CLAUDE_SESSION_LIMIT_MESSAGE_MARKERS
    )


def _is_poisoned_session_failure(
    backend: str, agent_result: AgentResult,
) -> bool:
    """True iff resuming this session is futile and a fresh spawn is the only
    recovery: the session was GC'd (stale) or its transcript overflowed the
    model context window. Both clear the pinned session id and retry once as
    a fresh spawn in `_resume_dev_with_text`.
    """
    return (
        _is_stale_session_failure(backend, agent_result)
        or _is_context_overflow_failure(backend, agent_result)
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

    session = _DevSession(*_read_dev_session(state))
    silent_count = int(state.get(_SILENT_PARK_COUNT) or 0)
    resume_count = int(state.get(_DEV_RESUME_COUNT) or 0)
    retirement_reason = _dev_session_retirement_reason(
        session.session_id, resume_count, silent_count,
    )
    if retirement_reason is not None:
        _wf.log.info(
            "issue=#%d retiring dev session %r (%s); starting fresh",
            issue.number, session.session_id, retirement_reason,
        )
        _drop_poisoned_dev_session(state)
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


@dataclass(frozen=True)
class _DevResumeContext:
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    followup_text: str
    options: _DevResumeOptions
    worktree: Path
    plan: _DevResumePlan
    stage: str

    @classmethod
    def build(
        cls,
        gh: GitHubClient,
        spec: config.RepoSpec,
        issue: Issue,
        resume_args: tuple,
        option_fields: dict,
        *,
        stage: Optional[str] = None,
    ) -> _DevResumeContext:
        if len(resume_args) != 2:
            raise TypeError("expected state and followup_text")
        state, followup_text = resume_args
        options = _DevResumeOptions.from_fields(option_fields)
        worktree = _ensure_resume_worktree(spec, issue, state)
        plan = _resolve_dev_session_for_resume(issue, state)
        # An explicit `stage` wins over the label read off `issue`: a caller
        # that just relabeled the issue (validating -> fixing on
        # CHANGES_REQUESTED) holds an `Issue` whose cached `labels` PyGithub
        # did not refresh after `set_labels`, so `gh.workflow_label(issue)`
        # would still report the pre-flip stage and misattribute the run.
        return cls(
            gh=gh,
            spec=spec,
            issue=issue,
            state=state,
            followup_text=followup_text,
            options=options,
            worktree=worktree,
            plan=plan,
            stage=stage or gh.workflow_label(issue) or _IMPLEMENTING_STAGE,
        )

    def _run_attempt(
        self, *, fresh: bool, session_id: Optional[str],
    ) -> tuple[AgentResult, bool]:
        from orchestrator import workflow as _wf

        session = self.plan.session
        agent_result = _wf._run_agent_tracked(
            self.gh,
            self.issue.number,
            agent_role="developer",
            stage=self.stage,
            backend=session.backend,
            prompt=_build_dev_spawn_prompt(
                self.spec,
                self.issue,
                self.followup_text,
                followup_has_tracked_repos=(
                    self.options.followup_has_tracked_repos
                ),
                fresh=fresh,
            ),
            cwd=self.worktree,
            agent_spec=session.spec,
            resume_session_id=session_id,
            extra_args=session.extra_args,
            review_round=self.state.get("review_round", 0),
            retry_count=self.state.get(_RETRY_COUNT),
        )
        _wf._accumulate_issue_usage(self.state, agent_result.usage)
        paused = (
            self.options.pause_guard
            and _wf._paused_during_agent_run(self.gh, self.issue)
        )
        return agent_result, paused

    def _needs_fresh_retry(self, agent_result: AgentResult) -> bool:
        return (
            self.plan.session.session_id is not None
            and not self.plan.fresh_spawn
            and _is_poisoned_session_failure(
                self.plan.session.backend, agent_result,
            )
        )

    def execute(self) -> Tuple[Path, AgentResult, bool]:
        from orchestrator import workflow as _wf

        agent_result, paused = self._run_attempt(
            fresh=self.plan.fresh_spawn,
            session_id=self.plan.session.session_id,
        )
        if paused:
            return self.worktree, agent_result, True
        fresh_spawn = self.plan.fresh_spawn
        if self._needs_fresh_retry(agent_result):
            _wf.log.info(
                "issue=#%d dropping poisoned dev session %r after poisoned-session "
                "marker (stale or context overflow); retrying once as a fresh spawn",
                self.issue.number, self.plan.session.session_id,
            )
            _drop_poisoned_dev_session(self.state)
            fresh_spawn = True
            agent_result, paused = self._run_attempt(
                fresh=True, session_id=None,
            )
            if paused:
                return self.worktree, agent_result, True
        _persist_dev_session_after_run(
            self.state,
            agent_result,
            fresh_spawn=fresh_spawn,
            resume_count=self.plan.resume_count,
        )
        return self.worktree, agent_result, False


def _ensure_resume_worktree(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Path:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    if worktree.exists():
        return worktree
    return _wf._ensure_worktree(
        spec,
        issue.number,
        branch=_wf._resolve_branch_name(state, spec, issue.number),
    )


def _resume_dev_with_text(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    *resume_args,
    stage: Optional[str] = None,
    **option_fields,
) -> Tuple[Path, AgentResult, bool]:
    """Resume the dev's locked-backend session with the given prompt text.

    `stage` overrides the recorded stage for every audit / analytics /
    trajectory record this run emits. It defaults to the label read off
    `issue`, which is correct whenever the caller fetched the issue fresh this
    tick. The CHANGES_REQUESTED fix path must pass it explicitly (`fixing`):
    it relabels validating -> fixing and then resumes on the SAME `Issue`
    object, whose cached `labels` PyGithub does not refresh after
    `set_labels`, so the label read would still report `validating` and
    attribute the developer run to the reviewer's stage.

    The backend is locked to whatever wrote `dev_session_id` (or the legacy
    `codex_session_id`) for this issue -- resuming across backends would need
    an inter-backend session bridge that does not exist. Clears the
    `awaiting_human` flag because the caller is reacting to a fresh human
    signal (issue or PR comment) by spawning the agent.

    After `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive `agent_silent`
    parks on the current `dev_session_id`, the resume drops the session id
    and starts a fresh spawn instead. Sessions killed mid-stream (e.g. by a
    Claude rate limit) consistently return empty results on every subsequent
    resume; without this fallback every human "retry" comment burns another
    fresh-spawn retry slot on the same poisoned session.

    Proactive rotation: each resume increments a per-session `dev_resume_count`
    and, once it reaches `config.DEV_SESSION_MAX_RESUMES` (when that knob is
    > 0), the session is retired and the spawn goes fresh. `--resume` replays
    the entire accumulated transcript every time, so a session resumed many
    times creeps toward the model context window; rotating proactively rebuilds
    a small prompt from durable state (issue body + recent comments + the
    committed branch) and caps that creep before it overflows. Every fresh
    spawn -- whether triggered by rotation, the silent-park fallback, or
    poisoned-session recovery -- is prefixed with a re-grounding preamble
    (`_build_fresh_respawn_preamble`) because the prior session's in-memory
    reasoning is gone and only its committed work survives on the branch.

    A Claude resume that comes back with `No conversation found with session
    ID` (or a sibling marker), or with a `Prompt is too long` context-window
    overflow, is treated as the same poisoned-session condition but
    recognized immediately: the pinned session id is cleared and the call is
    retried once as a fresh spawn in the same worktree, so a Claude session
    whose transcript was GC'd or grew past the context window doesn't park
    (`agent_silent` for two ticks, or `awaiting_human` forever) before
    recovering.

    Returns `(worktree, result, paused)`. `paused` is the live-pause decision
    -- True only when `pause_guard` is set AND a hard-skip control label
    (`paused` / `backlog`) was applied to a freshly fetched issue while an agent
    run was in flight. `pause_guard` is opt-in (default False): every
    developer-resume caller -- implementing, validating, documenting, in_review,
    fixing, and resolving_conflict -- passes it True and honors the flag. The
    check runs after BOTH agent runs -- the initial resume/spawn AND the
    poisoned-session fresh retry -- because each has its own live-pause window:
    the first fires before the retry spawns a second agent, and the second
    before the retry's result is persisted. When it fires the helper stops
    before the session id is persisted and before `awaiting_human` is cleared,
    and the caller must honor the returned flag by stopping too -- the decision
    is propagated, not re-fetched, so there is no window where the caller reads
    the label differently than the helper did.
    """
    return _DevResumeContext.build(
        gh, spec, issue, resume_args, option_fields, stage=stage,
    ).execute()


def _resume_developer_on_human_reply(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
    *,
    pause_guard: bool = False,
) -> Optional[Tuple[Path, AgentResult, bool]]:
    """Resume the developer's agent session with new issue-level comments.

    Returns (worktree, agent_result, paused) on resume, or None if there are no
    new comments since the last park (caller should return without writing
    state). `paused` is forwarded from `_resume_dev_with_text` and is only ever
    True when `pause_guard` is set; both callers (implementing and validating)
    pass it True and honor the flag.

    Used by `implementing` and `validating` -- both deliberately watch only
    the issue's comment thread, not the PR's. The `in_review` handler watches
    PR comments too via `_resume_dev_with_text` directly.

    Bumps `last_action_comment_id` to the highest consumed comment id BEFORE
    spawning the agent. Without this, a successful resume during implementing
    or validating leaves `last_action_comment_id` at the prior park id, so
    the validating->in_review handoff treats the just-consumed human reply
    as fresh PR feedback and re-resumes the dev on input it has already
    handled. This pre-resume bump is also robust to mid-resume failures:
    if the agent crashes or times out, those comments are still recorded
    as consumed (the dev DID see them via the resume prompt), and the
    failure is surfaced via the timeout/dirty/question paths instead.

    Untrusted authors are dropped up front so nothing they post drives the
    resume: with `ALLOWED_ISSUE_AUTHORS` set an outsider reply posted while the
    issue is parked awaiting human must not reach the dev prompt NOR advance the
    consumed watermark. Only trusted comments are consumed, so an outsider reply
    trailing a trusted one is left unconsumed rather than persisted as the
    watermark; an all-untrusted batch is treated as "no new reply".
    """
    last_action_id = state.get(_LAST_ACTION_COMMENT_ID)
    new_comments = filter_trusted(gh.comments_after(issue, last_action_id))
    if not new_comments:
        return None
    consumed_max = max(comment.id for comment in new_comments)
    state.set(_LAST_ACTION_COMMENT_ID, consumed_max)
    from orchestrator import workflow as _wf

    followup = "\n\n".join(
        _wf._quote_comment_line(comment)
        for comment in new_comments if comment.body
    )
    followup = f"{followup}\n\n{_wf._FOREGROUND_ONLY_NOTE}"
    return _resume_dev_with_text(
        gh, spec, issue, state, followup, pause_guard=pause_guard,
    )


def _publish_committed_work(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    work: _AgentWork,
) -> None:
    """Publish a worktree that carries a new commit.

    A clean tree pushes/opens the PR via `_on_commits`; a tree with
    uncommitted edits parks via `_on_dirty_worktree` (pushing would publish a
    branch that omits the dirty files). Shared by the fresh-completion, timeout,
    and user-content-drift dispositions so each handles a committed worktree
    identically.
    """
    from orchestrator import workflow as _wf

    dirty = _wf._worktree_dirty_files(work.worktree)
    if dirty:
        _on_dirty_worktree(gh, issue, state, work.agent_result, dirty)
    else:
        _on_commits(gh, spec, issue, state, work.agent_result)


def _park_agent_timeout(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    before_sha: Optional[str],
) -> None:
    """Park an implementer timeout that produced no publishable commit.

    Tags the park `agent_timeout` and persists the pre-agent SHA so the
    next-tick recovery (`_try_recover_implementing_timeout_park`) can publish a
    commit a lingering descendant finishes after this point without waiting for
    a human reply.
    """
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent timed out after "
        f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
        reason=_AGENT_TIMEOUT,
    )
    state.set(_PARK_REASON, _AGENT_TIMEOUT)
    state.set(_PRE_IMPLEMENT_SHA, before_sha or "")


def _try_recover_implementing_timeout_park(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> str:
    """Quietly publish a clean commit stranded by an implementer timeout.

    Implementing-stage counterpart to validating's
    `_try_recover_validating_transient_park`. An `agent_timeout` park can
    still carry a clean commit: a descendant the timeout cleanup raced
    finished writing it after disposition (the #77 shape, where the commit
    timestamp landed after the timeout event). Republish it through the
    normal commit path so a human does not have to manually clear
    `awaiting_human` to unstick the issue.

    Returns:
      * ``"pushed"`` -- a clean commit advanced past `pre_implement_sha` and
        was published via `_on_commits` (branch pushed, PR opened/reused,
        label -> validating, park flags cleared). Caller writes state.
      * ``"stuck"`` -- nothing safely recoverable (worktree reaped, dirty
        tree, missing watermark, or HEAD unchanged). Caller stays parked.

    Unlike validating's silent reviewer-rerun recovery this DOES post the
    normal ":sparkles: PR opened" comment via `_on_commits` -- publishing the
    branch is the entire point of the recovery. It must not spawn the agent.
    """
    from orchestrator import workflow as _wf

    wt = _wf._worktree_path(spec, issue.number)
    if not wt.exists():
        # Worktree reaped: the local commit is gone, nothing to publish.
        return _REASON_STUCK
    if _wf._worktree_dirty_files(wt):
        # A descendant left uncommitted edits; pushing would publish an
        # incomplete branch. Stay parked for human inspection.
        return _REASON_STUCK
    pre_sha = state.get(_PRE_IMPLEMENT_SHA)
    if not isinstance(pre_sha, str):
        # The timeout-tagging path always persists this; a missing watermark
        # is foreign state we cannot reason about, so stay parked rather than
        # risk publishing a branch we cannot vouch for.
        return _REASON_STUCK
    now_sha = _wf._head_sha(wt)
    if not now_sha or now_sha == pre_sha:
        # The timeout produced no new commit; stay parked for a human reply.
        return _REASON_STUCK
    # A clean commit advanced past the pre-timeout SHA. Clear the park flags
    # and publish it through the normal commit path.
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)
    state.set(_PRE_IMPLEMENT_SHA, None)
    _, _, _, dev_sid = _read_dev_session(state)
    agent_result = AgentResult(
        session_id=dev_sid,
        last_message=(
            "(orchestrator recovery: publishing commit produced around the "
            "agent timeout)"
        ),
        exit_code=0,
        timed_out=False,
        stdout="",
        stderr="",
    )
    _on_commits(gh, spec, issue, state, agent_result)
    return "pushed"


def _handle_stale_question_park(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> bool:
    """Clear a stale question-stage park left by a `question` -> `implementing`
    relabel, or refuse the relabel when it would ship question-agent work.

    `_handle_question` parks with `awaiting_human=True` and
    `park_reason="question_*"` so its own next tick can resume the locked
    question-agent session; those flags are opaque to implementing's resume
    path and would mis-fire it. When no such park is present this is a no-op
    returning False.

    The clear must check the actual worktree, NOT just the park reason. The
    question agent is supposed to be read-only, but a misbehaving run can park
    as `question_commits` / `question_dirty` (or `question_timeout` that
    committed before being killed) with unreviewed code state on the per-issue
    branch. Silently dropping the park would let the fresh-spawn branch's
    recovered-worktree shortcut (`_has_new_commits` -> push) publish the
    question agent's commits as if a dev session had authored them, violating
    the read-only contract.

    Returns True when the caller must return this tick: the unsafe relabel was
    re-parked as `question_unsafe_relabel` and pinned state written here. The
    branch check covers the case where the worktree was removed
    (`_cleanup_question_worktree` ran on a safe park, or the operator deleted
    the dir) but the local `orchestrator/<slug>/issue-N` branch survived with
    question-agent commits: `_ensure_worktree` would otherwise silently restore
    it and the recovered-worktree shortcut would ship those commits as a dev
    PR. The re-park is idempotent -- once `park_reason` is already
    `question_unsafe_relabel`, subsequent ticks stay silent until the state is
    cleaned or the operator relabels elsewhere.

    Returns False otherwise: either no question-stage park is present, or the
    worktree and branch are both clean so the relabel IS the unblock signal --
    the park flags are dropped and `last_action_comment_id` ratcheted past the
    question agent's answer comment (so the eventual validating->in_review
    watermark seed cannot replay it as fresh PR feedback) before the caller
    falls through to the fresh-spawn path.
    """
    park_reason = state.get(_PARK_REASON)
    if not (
        state.get(_AWAITING_HUMAN)
        and isinstance(park_reason, str)
        and park_reason.startswith("question_")
    ):
        return False
    hazard = _question_relabel_hazard(spec, issue, state)
    if hazard is not None:
        if park_reason != "question_unsafe_relabel":
            _park_unsafe_question_relabel(
                gh, issue, state, str(park_reason), hazard,
            )
        gh.write_pinned_state(issue, state)
        return True
    _clear_stale_question_park(gh, issue, state)
    return False


@dataclass(frozen=True)
class _QuestionRelabelHazard:
    branch: str
    trigger: str


def _question_relabel_hazard(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Optional[_QuestionRelabelHazard]:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    dirty = worktree.exists() and bool(_wf._worktree_dirty_files(worktree))
    unpushed = _wf._branch_has_unpushed_commits(spec, issue.number)
    if not dirty and not unpushed:
        return None
    branch = unpushed or _wf._resolve_branch_name(state, spec, issue.number)
    return _QuestionRelabelHazard(
        branch=branch,
        trigger=_question_relabel_trigger(dirty, bool(unpushed), branch),
    )


def _question_relabel_trigger(dirty: bool, unpushed: bool, branch: str) -> str:
    if dirty and not unpushed:
        return "dirty edits in the per-issue worktree"
    if unpushed and not dirty:
        return f"unreviewed commits on the per-issue branch `{branch}`"
    return (
        f"unreviewed commits on the per-issue branch `{branch}` "
        "AND dirty edits in its worktree"
    )


def _park_unsafe_question_relabel(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    park_reason: str,
    hazard: _QuestionRelabelHazard,
) -> None:
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} relabeled to `implementing`, "
        f"but the prior question-stage park (`{park_reason}`) left "
        f"{hazard.trigger}. The question agent must be read-only, so the "
        "orchestrator refuses to push that work as a dev implementation. "
        "Reset the worktree (e.g. `git -C <worktree> reset --hard "
        "origin/<base> && git -C <worktree> clean -fd`), or delete the "
        f"local branch (`git branch -D {hazard.branch}` in `target_root`), "
        "before re-relabeling so the dev agent starts from a clean base.",
        reason="question_unsafe_relabel",
    )
    state.set(_PARK_REASON, "question_unsafe_relabel")


def _clear_stale_question_park(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        prior = state.get(_LAST_ACTION_COMMENT_ID)
        if not isinstance(prior, int) or latest > prior:
            state.set(_LAST_ACTION_COMMENT_ID, latest)


def _retry_parked_dev_session(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    new_comments: list,
) -> None:
    """Resume the locked dev session as an intentional `/orchestrator continue`
    retry of a session-failure park (`agent_silent` / `agent_timeout`), then
    dispose the result exactly like the awaiting-human resume path.

    Unlike the generic human-reply resume this does NOT feed the bare command
    text to the dev (`_wf._CONTINUE_RETRY_PROMPT` instead): the poisoned session
    already carries the issue context in its transcript, or `_resume_dev_with_text`
    rotates it to a re-grounded fresh spawn. The command comment(s) are marked
    consumed up front so the retry does not re-fire next tick -- every fresh
    comment is a bare continue here (the classifier's retry precondition), so
    this drops no guidance. `user_content_hash` is deliberately NOT refreshed:
    a bare continue never shifts it, and masking it here would swallow a real
    body edit that landed in the same window before the dev could see it.
    """
    from orchestrator import workflow as _wf

    state.set(
        _LAST_ACTION_COMMENT_ID,
        max(comment.id for comment in new_comments),
    )
    wt = _wf._worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _wf._ensure_worktree(
            spec, issue.number,
            branch=_wf._resolve_branch_name(state, spec, issue.number),
        )
    before_sha = _wf._head_sha(wt)
    followup = f"{_wf._CONTINUE_RETRY_PROMPT}\n\n{_wf._FOREGROUND_ONLY_NOTE}"
    wt, agent_result, paused = _resume_dev_with_text(
        gh, spec, issue, state, followup, pause_guard=True,
    )
    state.set("last_agent_action_at", _wf._now_iso())
    state.set(_BRANCH, _wf._resolve_branch_name(state, spec, issue.number))
    # A shutdown-killed or live-paused resume leaves durable state untouched so
    # the next process re-detects and re-runs the retry (mirrors the drift and
    # fresh-spawn dispositions).
    if _wf._ignore_if_interrupted(issue, agent_result):
        return
    if paused:
        return
    _dispose_agent_result(
        gh, spec, issue, state,
        _PreparedDevRun(agent_result, before_sha, False, wt),
    )


def _handle_parked_continue_command(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    """Handle an operator `/orchestrator continue` on a parked `implementing`
    issue BEFORE generic user-content-drift / resume processing.

    `/orchestrator continue` is the recovery signal for a dev session that hit
    a session/usage limit or a silent failure (`_park_session_limit` /
    `_park_silent_failure` tag both `agent_silent`; an implementer timeout tags
    `agent_timeout`). Counting the bare command as an ordinary comment routed
    it through "issue body/content changed" drift handling and resumed the dev
    for the wrong reason (issue #729); a bare continue no longer shifts
    `user_content_hash`, and this handler routes it deliberately instead.

    Returns True when the command was fully handled this tick (an intentional
    retry ran, or a refusal was posted) and the caller must return. Returns
    False to fall through to the normal flow: the issue is not parked, the park
    belongs to the refresh-time rebase loop, there is no new comment, no
    continue command is present, or the command arrived alongside genuine
    guidance (which the normal resume / drift path feeds to the dev).
    """
    decision = _parked_continue_decision(gh, issue, state)
    if decision is None:
        return False
    if decision.action == "refuse":
        from orchestrator import workflow as _wf

        _wf._refuse_parked_continue(gh, issue, state)
        gh.write_pinned_state(issue, state)
    else:
        _retry_parked_dev_session(
            gh, spec, issue, state, decision.comments,
        )
    return True


@dataclass(frozen=True)
class _ParkedContinueDecision:
    action: str
    comments: list


def _parked_continue_decision(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> Optional[_ParkedContinueDecision]:
    from orchestrator import workflow as _wf

    if not state.get(_AWAITING_HUMAN):
        return None
    park_reason = state.get(_PARK_REASON)
    # Refresh-time auto-rebase parks own their operator retry comment.
    if park_reason in _wf._AUTO_REBASE_PARK_REASONS:
        return None
    comments = filter_trusted(
        gh.comments_after(issue, state.get(_LAST_ACTION_COMMENT_ID))
    )
    if not comments:
        return None
    action = _wf._continue_command_action(comments, park_reason)
    if action == "passthrough":
        return None
    return _ParkedContinueDecision(action, comments)


def _handle_user_content_drift(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    new_hash: str,
) -> bool:
    """React to a human editing the issue title/body after the dev spawned.

    Persists the new content hash, then:
      * With a recorded dev session -> notify the human, mark the current
        conversation consumed, resume the locked session with the updated
        requirements, and dispose the result (publish a fresh commit, park a
        commit-less timeout, ACK an explicit "existing work satisfies" reply,
        or park the question). Always returns True -- the caller must return.
      * Without a dev session but with recovered unpushed commits from a prior
        tick -> park `stale_recovered_work` (those commits never saw the edited
        body) and return True.
      * Without a dev session and without recovered commits -> clear any park
        and return False so the caller falls through to the fresh-spawn path,
        which builds the implement prompt from the current `issue.body`.

    The issue spec ("don't re-decompose mid-implementation -- too disruptive")
    rules out routing back to `decomposing`; the locked session decides what to
    do with the new body instead.
    """
    state.set("user_content_hash", new_hash)
    if state.get(_DEV_AGENT) or state.get(_CODEX_SESSION_ID):
        _resume_dev_on_implementing_drift(gh, spec, issue, state)
        return True
    return _handle_pre_session_drift(gh, spec, issue, state)


@dataclass(frozen=True)
class _ImplementingDriftRun:
    worktree: Path
    agent_result: AgentResult
    before_sha: Optional[str]
    paused: bool
    committed: bool


def _run_implementing_drift_resume(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> _ImplementingDriftRun:
    from orchestrator import workflow as _wf

    worktree = _ensure_resume_worktree(spec, issue, state)
    before_sha = _wf._head_sha(worktree)
    followup = _wf._build_user_content_change_prompt(
        issue, _wf._recent_comments_text(issue),
    )
    resumed = _resume_dev_with_text(
        gh, spec, issue, state, followup, pause_guard=True,
    )
    return _implementing_drift_run(before_sha, resumed)


def _implementing_drift_run(
    before_sha: Optional[str], resumed: Tuple[Path, AgentResult, bool],
) -> _ImplementingDriftRun:
    from orchestrator import workflow as _wf

    worktree, agent_result, paused = resumed
    after_sha = _wf._head_sha(worktree)
    return _ImplementingDriftRun(
        worktree=worktree,
        agent_result=agent_result,
        before_sha=before_sha,
        paused=paused,
        committed=bool(after_sha) and after_sha != before_sha,
    )


def _post_implementing_drift_ack(
    gh: GitHubClient, issue: Issue, state: PinnedState, reason: str,
) -> None:
    from orchestrator import workflow as _wf

    quoted = _as_blockquote(reason)
    _wf._post_issue_comment(
        gh, issue, state,
        ":speech_balloon: dev session reports the existing "
        f"work satisfies the edit:\n\n{quoted}",
    )
    state.set(_SILENT_PARK_COUNT, 0)


def _dispose_implementing_drift(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    drift: _ImplementingDriftRun,
) -> None:
    from orchestrator import workflow as _wf

    if (
        _wf._ignore_if_interrupted(issue, drift.agent_result)
        or drift.paused
    ):
        return
    if drift.committed:
        _publish_committed_work(
            gh, spec, issue, state,
            _AgentWork(drift.agent_result, drift.worktree),
        )
    elif drift.agent_result.timed_out:
        _park_agent_timeout(gh, issue, state, drift.before_sha)
    else:
        ack_reason = _wf._drift_ack_reason(
            drift.agent_result.last_message or "",
        )
        if ack_reason:
            _post_implementing_drift_ack(gh, issue, state, ack_reason)
        else:
            _on_question(gh, issue, state, drift.agent_result)
    gh.write_pinned_state(issue, state)


def _resume_dev_on_implementing_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> None:
    from orchestrator import workflow as _wf

    _wf._post_issue_comment(
        gh, issue, state,
        ":pencil2: issue body changed; resuming dev session with "
        "the updated requirements.",
    )
    _wf._mark_drift_comments_consumed(gh, issue, state)
    drift = _run_implementing_drift_resume(gh, spec, issue, state)
    state.set("last_agent_action_at", _wf._now_iso())
    state.set(_BRANCH, _wf._resolve_branch_name(state, spec, issue.number))
    _dispose_implementing_drift(gh, spec, issue, state, drift)


def _handle_pre_session_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    if _wf._has_new_commits(spec, worktree):
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} issue body changed but the "
            "worktree carries unpushed commits from a previous tick "
            "and no dev session is recorded. Refusing to push commits "
            "that never saw the edited requirements; decide whether "
            "to discard the recovered work (reset the branch) and "
            "let a fresh agent run, or accept it as-is.",
            reason="stale_recovered_work",
        )
        gh.write_pinned_state(issue, state)
        return True
    if state.get(_AWAITING_HUMAN):
        _wf._post_issue_comment(
            gh, issue, state,
            ":pencil2: issue content changed; clearing the park and "
            "spawning a fresh dev run against the updated requirements.",
        )
        _wf._mark_drift_comments_consumed(gh, issue, state)
        state.set(_AWAITING_HUMAN, False)
        state.set(_PARK_REASON, None)
    return False


def _recover_quiet_implementer_timeout(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    if state.get(_PARK_REASON) != _AGENT_TIMEOUT:
        return False
    comments = filter_trusted(
        gh.comments_after(issue, state.get(_LAST_ACTION_COMMENT_ID))
    )
    if comments:
        return False
    if _try_recover_implementing_timeout_park(gh, spec, issue, state) == "pushed":
        gh.write_pinned_state(issue, state)
    return True


def _prepare_awaiting_dev_run(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Optional[_PreparedDevRun]:
    from orchestrator import workflow as _wf

    if _recover_quiet_implementer_timeout(gh, spec, issue, state):
        return None
    worktree = _ensure_resume_worktree(spec, issue, state)
    before_sha = _wf._head_sha(worktree)
    resumed = _resume_developer_on_human_reply(
        gh, spec, issue, state, pause_guard=True,
    )
    if resumed is None:
        return None
    worktree, agent_result, paused = resumed
    return _PreparedDevRun(agent_result, before_sha, paused, worktree)


def _recovered_dev_result(state: PinnedState) -> AgentResult:
    return AgentResult(
        session_id=_read_dev_session(state)[3],
        last_message="(orchestrator restart: pushing previously committed work)",
        exit_code=0,
        timed_out=False,
        stdout="",
        stderr="",
    )


def _spawn_implementer(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    worktree: Path,
) -> Optional[tuple[AgentResult, bool]]:
    from orchestrator import workflow as _wf

    if not _check_and_increment_retry_budget(gh, issue, state):
        gh.write_pinned_state(issue, state)
        return None
    session = _DevSession(*_read_dev_session(state))
    state.set(_DEV_AGENT, session.spec)
    agent_result = _wf._run_agent_tracked(
        gh,
        issue.number,
        agent_role="developer",
        stage=_IMPLEMENTING_STAGE,
        backend=session.backend,
        prompt=_wf._build_implement_prompt(
            spec,
            issue,
            _wf._recent_comments_text(issue),
            config.default_repo_specs(),
        ),
        cwd=worktree,
        agent_spec=session.spec,
        extra_args=session.extra_args,
        review_round=state.get("review_round", 0),
        retry_count=state.get(_RETRY_COUNT),
    )
    _wf._accumulate_issue_usage(state, agent_result.usage)
    if agent_result.session_id:
        state.set(_DEV_SESSION_ID, agent_result.session_id)
        state.set(_DEV_RESUME_COUNT, 0)
    return agent_result, _wf._paused_during_agent_run(gh, issue)


def _prepare_active_dev_run(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Optional[_PreparedDevRun]:
    from orchestrator import workflow as _wf

    worktree = _wf._ensure_worktree(
        spec,
        issue.number,
        branch=_wf._resolve_branch_name(state, spec, issue.number),
    )
    before_sha = _wf._head_sha(worktree)
    if _wf._has_new_commits(spec, worktree):
        _wf.log.info(
            "issue=#%d skipping agent; worktree already has commits",
            issue.number,
        )
        return _PreparedDevRun(
            _recovered_dev_result(state), before_sha, False, worktree,
        )
    spawned = _spawn_implementer(gh, spec, issue, state, worktree)
    if spawned is None:
        return None
    agent_result, paused = spawned
    return _PreparedDevRun(agent_result, before_sha, paused, worktree)


def _prepare_dev_run(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> Optional[_PreparedDevRun]:
    """Set up and run (or recover) the dev agent for one implementing tick.

    Returns a prepared run for the caller to dispose, or None
    when the tick is already complete and the caller must return:
      * awaiting-human with an `agent_timeout` park and no human reply -> a
        silent `_try_recover_implementing_timeout_park` attempt (state written
        here on "pushed", left parked on "stuck");
      * awaiting-human resume with no new comments -> nothing to do;
      * a fresh spawn blocked by the 24h retry cap (parked, state written).

    `before_sha` is the pre-agent HEAD watermark the timeout disposition uses
    to tell a commit produced by THIS run from carried-over commits already on
    the branch.
    """
    from orchestrator import workflow as _wf

    if state.get(_AWAITING_HUMAN):
        prepared = _prepare_awaiting_dev_run(gh, spec, issue, state)
    else:
        prepared = _prepare_active_dev_run(gh, spec, issue, state)
    if prepared is not None:
        state.set(
            _BRANCH, _wf._resolve_branch_name(state, spec, issue.number),
        )
    return prepared


def _dispose_agent_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    prepared: _PreparedDevRun,
) -> None:
    """Dispose a completed implementing run and write pinned state.

    A timed-out run publishes a commit produced by THIS run (clean tree), parks
    a dirty tree for inspection, or parks `agent_timeout` when HEAD did not
    advance past `before_sha`. A clean exit publishes new commits or parks the
    agent's question. `before_sha` (not `_has_new_commits`, which only compares
    to `origin/<base>`) is what distinguishes a commit produced by THIS run
    from carried-over commits already on the branch.
    """
    from orchestrator import workflow as _wf

    if prepared.agent_result.timed_out:
        # The implementer can commit clean work and then get killed by the
        # timeout (or a descendant finishes the commit during cleanup). Don't
        # strand that commit behind `awaiting_human`: publish it if HEAD
        # advanced and the tree is clean, park a dirty tree for inspection, or
        # park as a timeout when it did not advance.
        after_sha = _wf._head_sha(prepared.worktree)
        if after_sha and after_sha != prepared.before_sha:
            _publish_committed_work(
                gh,
                spec,
                issue,
                state,
                _AgentWork(prepared.agent_result, prepared.worktree),
            )
        else:
            _park_agent_timeout(gh, issue, state, prepared.before_sha)
        gh.write_pinned_state(issue, state)
        return

    if _wf._has_new_commits(spec, prepared.worktree):
        _publish_committed_work(
            gh,
            spec,
            issue,
            state,
            _AgentWork(prepared.agent_result, prepared.worktree),
        )
    else:
        _on_question(gh, issue, state, prepared.agent_result)
    gh.write_pinned_state(issue, state)


def _implementing_preflight(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    from orchestrator import workflow as _wf

    if _wf._finalize_if_pr_merged(gh, spec, issue, state):
        return True
    if _wf._finalize_if_issue_closed(gh, spec, issue, state):
        return True
    if _handle_stale_question_park(gh, spec, issue, state):
        return True
    if _handle_parked_continue_command(gh, spec, issue, state):
        return True
    return False


def _handle_detected_implementing_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    from orchestrator import workflow as _wf

    new_hash = _wf._detect_user_content_change(gh, issue, state)
    return new_hash is not None and _handle_user_content_drift(
        gh, spec, issue, state, new_hash,
    )


def _handle_implementing(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    if _implementing_preflight(gh, spec, issue, state):
        return

    # User-content drift: a human edited the issue title/body after the dev
    # session was spawned. `_handle_user_content_drift` persists the new hash
    # and either resumes the locked session against the updated requirements
    # (returning True), parks recovered pre-edit work, or -- when no dev
    # session exists yet -- clears any park and returns False so the fresh-
    # spawn path below picks up the new body via `_build_implement_prompt`.
    if _handle_detected_implementing_drift(gh, spec, issue, state):
        return

    prepared = _prepare_dev_run(gh, spec, issue, state)
    if prepared is None:
        return

    state.set("last_agent_action_at", _wf._now_iso())

    # Shutdown-sweep interruption: a run the orchestrator killed mid-flight
    # has no trustworthy result, so ignore it and return WITHOUT writing
    # pinned state (the in-memory `awaiting_human=False` / watermark / session
    # mutations in `_prepare_dev_run` are discarded) so the next process
    # retries from durable state. Must precede the disposition below.
    if (
        _wf._ignore_if_interrupted(issue, prepared.agent_result)
        or prepared.paused
    ):
        return

    _dispose_agent_result(gh, spec, issue, state, prepared)


# GitHub rejects PR (and issue) bodies longer than 65,536 characters. The dev
# agent's final message is the only unbounded section appended to a PR body, so
# cap it well under that ceiling -- leaving headroom for the traceability
# header, the truncation marker, and GitHub's own rendering. The old 2000-char
# slice was an internal product choice, not a GitHub limit, and clipped most
# messages needlessly while leaving no sign text was dropped (issue #499).
_PR_BODY_AGENT_MESSAGE_CAP = 60000

# Appended after a trimmed message so the reader can tell content was dropped;
# a raw character slice ended the body mid-sentence with no such indication.
_PR_BODY_TRUNCATION_MARKER = "_…(message truncated)_"


def _format_pr_agent_message(
    message: str, *, cap: int = _PR_BODY_AGENT_MESSAGE_CAP
) -> str:
    """Return the agent's final message ready to embed in a PR body.

    A message within `cap` is returned verbatim. A longer one is trimmed on the
    nearest paragraph -> line -> word boundary before `cap` and an explicit
    `_…(message truncated)_` marker is appended, so the PR body reads as
    intentionally clipped rather than severed mid-sentence. A dangling code
    fence in the trimmed region is closed first so the marker (and any following
    body) renders outside the half-open block instead of being swallowed by it.
    """
    if len(message) <= cap:
        return message
    head = message[:cap]
    # Prefer a paragraph break, then a line break, then a word boundary, so the
    # cut lands somewhere readable instead of mid-token.
    for sep in ("\n\n", "\n", " "):
        idx = head.rfind(sep)
        if idx > 0:
            head = head[:idx]
            break
    head = head.rstrip()
    # An odd count of ``` fences means the cut landed inside a fenced block;
    # close it so GitHub doesn't swallow the marker into the open code block.
    if head.count("```") % 2:
        head = f"{head}\n```"
    return f"{head}\n\n{_PR_BODY_TRUNCATION_MARKER}"


def _derive_pr_title(spec: config.RepoSpec, issue: Issue, wt: Path) -> str:
    """PR title for a freshly opened dev PR.

    Prefers the first commit's conventional subject; when that carries no
    recognizable `<type>:` prefix, one is inferred from recent base-branch
    history (`_infer_subject_prefix`) and applied to the issue title.
    """
    from orchestrator import workflow as _wf

    first_subject = _wf._first_commit_subject(spec, wt)
    fallback_prefix = _wf._infer_subject_prefix(spec, wt, issue)
    return _wf._pr_title_from_commit_or_issue(
        issue, first_subject, fallback_prefix,
    )


def _build_pr_body(
    state: PinnedState, issue: Issue, agent_result: AgentResult,
) -> str:
    """PR body: the `Resolves #N` line, the generating session's identity, and
    the (capped) final agent message when the run produced one."""
    _, dev_backend, _, dev_sid = _read_dev_session(state)
    session_id = dev_sid or "?"
    body_parts = [
        f"Resolves #{issue.number}",
        "",
        f"Generated by orchestrator ({dev_backend} session `{session_id}`).",
    ]
    if agent_result.last_message.strip():
        body_parts += [
            "", "---", "_Last agent message:_", "",
            _format_pr_agent_message(agent_result.last_message),
        ]
    return "\n".join(body_parts)


def _reuse_or_open_pr(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    work: _PRWork,
):
    """Return the PR for `branch`, reusing an open one or opening a new one.

    Recovers gracefully if a previous tick crashed between `open_pr` and the
    relabel: an existing open PR is reused instead of 422-ing on a duplicate.
    Opening a new PR posts the ":sparkles: PR opened" comment and emits the
    `pr_opened` event; reuse only logs.
    """
    from orchestrator import workflow as _wf

    pr = gh.find_open_pr(branch=work.branch, base=spec.base_branch)
    if pr is not None:
        _wf.log.info(
            "issue=#%s reusing existing PR #%d for %s",
            issue.number, pr.number, work.branch,
        )
        return pr
    pr = gh.open_pr(
        branch=work.branch, base=spec.base_branch,
        title=_derive_pr_title(spec, issue, work.worktree),
        body=_build_pr_body(state, issue, work.agent_result),
    )
    _wf._post_issue_comment(gh, issue, state, f":sparkles: PR opened: #{pr.number}")
    gh.emit_event(
        "pr_opened",
        issue_number=issue.number,
        stage=_IMPLEMENTING_STAGE,
        pr_number=pr.number,
        branch=work.branch,
        sha=getattr(pr.head, "sha", None) or None,
        retry_count=state.get(_RETRY_COUNT),
    )
    return pr


def _advance_to_validating(
    gh: GitHubClient, issue: Issue, state: PinnedState, pr, branch: str
) -> None:
    """Record the published PR/branch, reset the per-PR budgets, and hand off
    to `validating`.

    The docs pass runs only as the final-docs handoff after the reviewer agent
    approves, so a fresh commit goes straight to validating.
    """
    state.set("pr_number", pr.number)
    # Persist the pushed branch alongside `pr_number` so the next tick's
    # `_resolve_branch_name` can recover it directly. Without this, a state
    # that lacked `branch` going in (e.g. an awaiting-human resume that opened
    # the PR here without first passing through the fresh-spawn branch-persist
    # site) would leave `pr_number` set with `branch` unset; the legacy-PR
    # fallback in `_resolve_branch_name` would then misroute every downstream
    # tick to `orchestrator/issue-<n>` while the live PR is on the
    # slug-namespaced branch this push just published.
    state.set(_BRANCH, branch)
    _reset_implementing_counters(state)
    gh.set_workflow_label(issue, WorkflowLabel.VALIDATING)


def _reset_implementing_counters(state: PinnedState) -> None:
    # Reset the review counter every time we (re-)open a PR so the validating
    # handler starts fresh on the new branch state.
    state.set("review_round", 0)
    # Issue moved forward; reset the implementing retry budget so any future
    # bounce back into implementing (e.g. validating -> implementing in a
    # later stage) starts with a fresh window.
    state.set(_RETRY_COUNT, 0)
    state.set(_RETRY_WINDOW_START, None)
    # The session just produced commits, so it isn't poisoned -- reset the
    # silent-park streak so a future blip doesn't tip an otherwise-healthy
    # session past the fresh-session threshold.
    state.set(_SILENT_PARK_COUNT, 0)
    # The commit shipped, so any agent-timeout park watermark is spent -- clear
    # it (and the stale reason) so it cannot linger into `validating` or
    # mis-fire the next-tick timeout recovery on a later implementing hop.
    if state.get(_PARK_REASON) == _AGENT_TIMEOUT:
        state.set(_PARK_REASON, None)
    state.set(_PRE_IMPLEMENT_SHA, None)


def _on_commits(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    agent_result: AgentResult,
) -> None:
    from orchestrator import workflow as _wf

    wt = _wf._worktree_path(spec, issue.number)
    branch = _wf._resolve_branch_name(state, spec, issue.number)
    if not _wf._push_branch(spec, wt, branch):
        # Park on awaiting_human like the timeout/question paths. Otherwise the
        # worktree's commits keep _has_new_commits() true, so every poll would
        # re-enter _on_commits() and re-comment indefinitely until a human acts.
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
            reason="push_failed",
        )
        # _handle_implementing writes pinned state after we return.
        return
    pr = _reuse_or_open_pr(
        gh, spec, issue, state, _PRWork(agent_result, wt, branch),
    )
    _advance_to_validating(gh, issue, state, pr, branch)


def _mark_agent_silent_park(state: PinnedState) -> None:
    """Flag a retryable `agent_silent` park and advance the silent-park streak.

    Shared by the session-limit and empty-output parks: both are retryable
    `agent_silent` failures, not real questions. `_resume_dev_with_text` reads
    the streak (via `_dev_session_retirement_reason`) to rotate a poisoned
    session to a fresh spawn once it reaches `_SILENT_PARKS_BEFORE_FRESH_SESSION`.
    """
    count = int(state.get(_SILENT_PARK_COUNT) or 0)
    state.set(_AWAITING_HUMAN, True)
    state.set(_PARK_REASON, "agent_silent")
    state.set(_SILENT_PARK_COUNT, count + 1)


def _park_session_limit(
    gh: GitHubClient, issue: Issue, state: PinnedState, raw: str
) -> str:
    """Park a session/usage-quota notice as a RETRYABLE session failure.

    A known quota notice ("You've hit your session limit ...") is non-empty but
    is NOT a real agent question: the session is healthy, the account quota is
    exhausted, and the only recovery is to wait for the reset and retry.
    Parking it as `agent_silent` (the same reason a silent poisoned resume
    uses) lets an operator's `/orchestrator continue` after the reset drop the
    session and re-ground a fresh one; classifying it as a real question
    (`park_reason=None`) would refuse that continue as "needs your actual
    guidance". The silent-park streak is incremented so a session that keeps
    returning the quota notice is eventually rotated, mirroring the
    empty-message branch. Returns the distinct EVENT reason
    (`agent_session_limit`) for observability -- the pinned `park_reason` stays
    `agent_silent` (the control field `/orchestrator continue` keys off).
    """
    from orchestrator import workflow as _wf

    quoted = _as_blockquote(raw)
    _wf._post_issue_comment(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent hit a session/usage limit and "
        "stopped; retry with `/orchestrator continue` once it "
        f"resets:\n\n{quoted}",
    )
    _mark_agent_silent_park(state)
    return "agent_session_limit"


def _park_real_question(
    gh: GitHubClient, issue: Issue, state: PinnedState, raw: str
) -> str:
    """Park a genuine agent clarification question awaiting a human reply."""
    from orchestrator import workflow as _wf

    quoted = _as_blockquote(raw)
    _wf._post_issue_comment(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent needs your input to proceed:\n\n{quoted}",
    )
    state.set(_AWAITING_HUMAN, True)
    # Real question parks are not transient: they need a human reply before the
    # in_review ready-ping gates should run again. Clear any stale
    # `park_reason` left behind by a prior in_review unmergeable park, and reset
    # the silent-park streak.
    state.set(_PARK_REASON, None)
    state.set(_SILENT_PARK_COUNT, 0)
    return "agent_question"


def _park_silent_failure(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    agent_result: AgentResult,
) -> str:
    """Park a run that produced no commit AND no message as a silent failure.

    Callers only invoke `_on_question` when the worktree has no new commits, so
    an empty `last_message` is a silent failure, not a content question -- most
    often a poisoned resume of a session killed mid-stream (e.g. by a Claude
    rate limit). Tag the park `agent_silent` so `_resume_dev_with_text` can
    drop the dev session id after enough consecutive silent parks, and surface
    the situation accurately instead of impersonating a real question park.
    """
    from orchestrator import workflow as _wf

    diag = _wf._format_stderr_diagnostics(agent_result, "Agent")
    _wf._post_issue_comment(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent produced no output (likely a "
        f"session-resume failure); manual intervention needed.{diag}",
    )
    _wf.log.warning(
        "issue=#%s agent produced no output; exit_code=%d "
        "timed_out=%s stderr_tail=%r",
        issue.number, agent_result.exit_code, agent_result.timed_out,
        _wf._stderr_log_tail(agent_result),
    )
    _mark_agent_silent_park(state)
    return "agent_silent"


def _on_question(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    agent_result: AgentResult,
) -> None:
    raw = agent_result.last_message.strip()
    if raw and _is_session_limit_message(agent_result):
        park_reason = _park_session_limit(gh, issue, state, raw)
    elif raw:
        park_reason = _park_real_question(gh, issue, state, raw)
    else:
        park_reason = _park_silent_failure(gh, issue, state, agent_result)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set(_LAST_ACTION_COMMENT_ID, latest)
    gh.emit_event(
        "park_awaiting_human",
        issue_number=issue.number,
        stage=gh.workflow_label(issue),
        reason=park_reason,
    )


def _on_dirty_worktree(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    agent_result: AgentResult,
    dirty: list[str],
) -> None:
    """Park instead of pushing when the agent left uncommitted changes.

    Pushing here would publish a branch that omits the dirty files, so the PR
    would not match what the agent actually produced. We surface the situation
    to the human and resume the codex session on their reply, identical to the
    question path.
    """
    from orchestrator import workflow as _wf

    _wf._post_issue_comment(
        gh, issue, state, _dirty_worktree_message(agent_result, dirty),
    )
    state.set(_AWAITING_HUMAN, True)
    # Mirror `_on_question`: this needs human input, so stale transient state
    # must not auto-recover over it.
    state.set(_PARK_REASON, None)
    state.set(_SILENT_PARK_COUNT, 0)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set(_LAST_ACTION_COMMENT_ID, latest)
    gh.emit_event(
        "park_awaiting_human",
        issue_number=issue.number,
        stage=gh.workflow_label(issue),
        reason="dirty_worktree",
        dirty_files=len(dirty),
    )


def _dirty_worktree_message(
    agent_result: AgentResult, dirty: list[str],
) -> str:
    shown = dirty[:10]
    files_md = "\n".join(f"- `{file_path}`" for file_path in shown)
    if len(dirty) > len(shown):
        elided = len(dirty) - len(shown)
        files_md = f"{files_md}\n- … ({elided} more)"
    last_msg = agent_result.last_message.strip()
    tail = ""
    if last_msg:
        tail = f"\n\n_Last agent message:_\n\n{_as_blockquote(last_msg)}"
    return (
        f"{config.HITL_MENTIONS} agent committed but left {len(dirty)} "
        f"uncommitted change(s); refusing to push an incomplete branch. "
        f"Reply with guidance and the orchestrator will resume the session.\n\n"
        f"{files_md}{tail}"
    )
