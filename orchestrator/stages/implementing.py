# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing stage handlers and developer-session lifecycle.

Owns `_handle_implementing` plus the developer-side primitives the rest
of the workflow re-uses: per-issue dev session lookup, resume on human
reply, poisoned-session recovery, stale-session detection, the 24h
retry budget, and the post-agent disposition helpers (`_on_commits`,
`_on_question`, `_on_dirty_worktree`).

ALL workflow-owned helpers (`_park_awaiting_human`, `_run_agent_tracked`,
`_now_iso`, the worktree plumbing, the drift / manifest / messaging
helpers re-exported into `workflow`) are reached through the parent
module via `from .. import workflow as _wf` at call time. The
compatibility surface tests rely on -- `patch.object(workflow, "_foo")`
-- has to keep working from inside the stage module too, so the
handlers must NOT direct-import these names from `workflow_drift` /
`workflow_messages` / `worktrees`; doing so would bind a stable
reference that test patches against `workflow.X` could not affect.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from github.Issue import Issue

from .. import config
from ..agents import AgentResult
from ..config import RepoSpec
from ..state_machine import WorkflowLabel
from ..github import GitHubClient, PinnedState


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
    stored = state.get("dev_agent")
    if stored:
        spec = str(stored)
        backend, args = config._parse_agent_spec("dev_agent", spec)
        sid = state.get("dev_session_id")
        return spec, backend, args, str(sid) if sid is not None else None
    legacy = state.get("codex_session_id")
    if legacy is not None:
        return "codex", "codex", (), str(legacy)
    return (
        config.DEV_AGENT_SPEC,
        config.DEV_AGENT,
        config.DEV_AGENT_ARGS,
        None,
    )


def _is_stale_session_failure(backend: str, result: AgentResult) -> bool:
    """True iff `result` is a deterministic stale-session resume failure.

    Only claude is matched today: codex's resume CLI does not expose a
    comparable stable stderr marker, so codex still relies on the silent-
    park-count fallback. If/when codex grows one, add it here.
    """
    if backend != "claude":
        return False
    stderr = (result.stderr or "").lower()
    if not stderr:
        return False
    return any(marker in stderr for marker in _CLAUDE_STALE_SESSION_STDERR_MARKERS)


def _is_context_overflow_failure(backend: str, result: AgentResult) -> bool:
    """True iff `result` is a Claude context-window-overflow resume failure.

    Only claude is matched today: codex's resume CLI does not expose a
    comparable stable marker. The marker is checked as a PREFIX of the
    stripped, lowercased last agent message -- so an agent that merely
    mentions the phrase mid-answer is not misclassified -- and as a substring
    of stderr, where the CLI may print the same diagnostic when it produces
    no result event at all.
    """
    if backend != "claude":
        return False
    msg = (result.last_message or "").strip().lower()
    if any(msg.startswith(marker) for marker in _CLAUDE_CONTEXT_OVERFLOW_MARKERS):
        return True
    stderr = (result.stderr or "").lower()
    return any(marker in stderr for marker in _CLAUDE_CONTEXT_OVERFLOW_MARKERS)


def _is_poisoned_session_failure(backend: str, result: AgentResult) -> bool:
    """True iff resuming this session is futile and a fresh spawn is the only
    recovery: the session was GC'd (stale) or its transcript overflowed the
    model context window. Both clear the pinned session id and retry once as
    a fresh spawn in `_resume_dev_with_text`.
    """
    return (
        _is_stale_session_failure(backend, result)
        or _is_context_overflow_failure(backend, result)
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
    if not state.get("dev_agent") and state.get("codex_session_id") is not None:
        state.set("dev_agent", "codex")
    state.set("dev_session_id", None)
    state.set("codex_session_id", None)
    state.set("silent_park_count", 0)
    # The resume budget is per-session; clearing the session resets it so the
    # fresh spawn that follows starts its own count from zero.
    state.set("dev_resume_count", 0)


def _check_and_increment_retry_budget(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    stage: str = "implementing",
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
    from .. import workflow as _wf
    from datetime import datetime, timedelta, timezone

    cap = config.MAX_RETRIES_PER_DAY
    if cap <= 0:
        return True

    now = datetime.now(timezone.utc)
    window_start_raw = state.get("retry_window_start")
    window_start: Optional[datetime] = None
    if window_start_raw:
        try:
            window_start = datetime.fromisoformat(window_start_raw)
        except (TypeError, ValueError):
            window_start = None

    if window_start is None or now - window_start > timedelta(hours=24):
        # Window absent/corrupt/expired: open a new one.
        state.set("retry_window_start", _wf._now_iso())
        state.set("retry_count", 0)
        window_start_raw = state.get("retry_window_start")

    count = int(state.get("retry_count") or 0)
    if count >= cap:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} hit retry cap ({cap}/day) for "
            f"{stage}; manual intervention needed. "
            f"Window opened at {window_start_raw}.",
            reason="retry_cap",
        )
        return False

    state.set("retry_count", count + 1)
    return True


def _resume_dev_with_text(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    followup_text: str,
) -> Tuple[Path, AgentResult]:
    """Resume the dev's locked-backend session with the given prompt text.

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
    """
    from .. import workflow as _wf

    wt = _wf._worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _wf._ensure_worktree(
            spec, issue.number,
            branch=_wf._resolve_branch_name(state, spec, issue.number),
        )
    dev_spec, dev_backend, dev_args, dev_sid = _read_dev_session(state)
    silent_count = int(state.get("silent_park_count") or 0)
    resume_count = int(state.get("dev_resume_count") or 0)
    # Proactive rotation: a session resumed past its budget carries a large
    # replayed transcript that creeps toward the context window. Retire it and
    # rebuild from durable state. 0 disables (resume forever).
    max_resumes = config.DEV_SESSION_MAX_RESUMES
    budget_exhausted = (
        dev_sid is not None
        and max_resumes > 0
        and resume_count >= max_resumes
    )
    silent_exhausted = (
        dev_sid is not None
        and silent_count >= _SILENT_PARKS_BEFORE_FRESH_SESSION
    )
    if budget_exhausted or silent_exhausted:
        _wf.log.info(
            "issue=#%d retiring dev session %r (%s); starting fresh",
            issue.number, dev_sid,
            f"resume budget reached: {resume_count} >= {max_resumes}"
            if budget_exhausted
            else f"{silent_count} consecutive silent parks",
        )
        dev_sid = None
        # Clear the retired session from pinned state BEFORE the spawn.
        # If the fresh spawn returns no `session_id` (or its persistence
        # is racy), the next tick must see a cleared session -- not the
        # old id, which `_read_dev_session` would otherwise return again
        # and burn another retry. Also resets `dev_resume_count` to 0.
        _drop_poisoned_dev_session(state)

    # `dev_sid is None` here means the spawn below opens a NEW session, not a
    # resume -- either rotation just retired the old one, or there was no live
    # session to resume on entry: the documenting initial pass, or a prior
    # backend hiccup that committed work but dropped `dev_session_id` while
    # leaving `dev_agent` pinned. A new session has no transcript, so it is
    # re-grounded (preamble below) and its returned id is persisted; it is NOT
    # charged against the resume budget, whose checks require a non-None
    # session id and so would otherwise never rotate the freshly pinned id.
    fresh_spawn = dev_sid is None

    # A fresh spawn has no transcript, so it must be re-grounded in the issue
    # requirements + conversation and pointed at the committed branch (where
    # the retired session's work survives). A resume already carries that
    # context in its transcript, so it gets the bare followup.
    def _spawn_prompt(fresh: bool) -> str:
        if not fresh:
            return followup_text
        preamble = _wf._build_fresh_respawn_preamble(
            issue, _wf._recent_comments_text(issue),
        )
        return f"{preamble}\n\n{followup_text}"

    # Stage context reflects the current label so events from validating /
    # in_review / resolving_conflict resumes (or implementing awaiting-human
    # resumes) are tagged with the handler that triggered the resume.
    resume_stage = gh.workflow_label(issue) or "implementing"
    result = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="developer",
        stage=resume_stage,
        backend=dev_backend,
        prompt=_spawn_prompt(fresh_spawn),
        cwd=wt,
        agent_spec=dev_spec,
        resume_session_id=dev_sid,
        extra_args=dev_args,
        review_round=state.get("review_round", 0),
        retry_count=state.get("retry_count"),
    )

    # Deterministic poisoned-session recovery: if we resumed with a session
    # id and Claude reported either a stale session ("no conversation found")
    # or a context-window overflow ("Prompt is too long"), the pinned session
    # is unrecoverable -- every further resume re-fails identically. Drop it
    # and retry once as a fresh spawn in the same worktree so the caller
    # (typically resolving_conflict awaiting-human) sees a real agent result
    # on this tick instead of a silent park or an endless "needs your input"
    # loop. Bounded to one retry: if the fresh spawn ALSO trips a poisoned-
    # session marker something deeper is wrong (a misconfigured CLI, or an
    # issue body so large even a fresh prompt overflows) and we surface that
    # result rather than looping.
    if (
        dev_sid is not None
        and not fresh_spawn
        and _is_poisoned_session_failure(dev_backend, result)
    ):
        _wf.log.info(
            "issue=#%d dropping poisoned dev session %r after poisoned-session "
            "marker (stale or context overflow); retrying once as a fresh spawn",
            issue.number, dev_sid,
        )
        _drop_poisoned_dev_session(state)
        fresh_spawn = True
        result = _wf._run_agent_tracked(
            gh, issue.number,
            agent_role="developer",
            stage=resume_stage,
            backend=dev_backend,
            prompt=_spawn_prompt(True),
            cwd=wt,
            agent_spec=dev_spec,
            resume_session_id=None,
            extra_args=dev_args,
            review_round=state.get("review_round", 0),
            retry_count=state.get("retry_count"),
        )

    if fresh_spawn:
        # Fresh spawn produced a session id -- record it so subsequent resumes
        # pick up the live session, and zero the resume budget so the new
        # session starts its own count. Mirrors the persistence done in
        # `_handle_implementing`'s fresh-spawn branch. Rotation / poisoned-
        # session recovery already reset `dev_resume_count` to 0 via
        # `_drop_poisoned_dev_session`; the explicit reset here also covers the
        # entry case (no live session on entry) where a stale count left by a
        # prior session would otherwise rotate the brand-new session early.
        if result.session_id:
            state.set("dev_session_id", result.session_id)
            state.set("dev_resume_count", 0)
    else:
        # A live session was resumed -- charge it against the resume budget so
        # the next tick can rotate once the transcript has grown enough.
        state.set("dev_resume_count", resume_count + 1)
    state.set("awaiting_human", False)
    return wt, result


def _resume_developer_on_human_reply(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState
) -> Optional[Tuple[Path, AgentResult]]:
    """Resume the developer's agent session with new issue-level comments.

    Returns (worktree, agent_result) on resume, or None if there are no new
    comments since the last park (caller should return without writing state).

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
    """
    last_action_id = state.get("last_action_comment_id")
    new_comments = gh.comments_after(issue, last_action_id)
    if not new_comments:
        return None
    consumed_max = max(c.id for c in new_comments)
    state.set("last_action_comment_id", consumed_max)
    from .. import workflow as _wf

    followup = "\n\n".join(
        f"@{c.user.login if c.user else 'user'}: {c.body}"
        for c in new_comments if c.body
    )
    followup = f"{followup}\n\n{_wf._FOREGROUND_ONLY_NOTE}"
    return _resume_dev_with_text(gh, spec, issue, state, followup)


def _handle_implementing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)

    # External merge short-circuit: a human merged the PR (or the PR was
    # merged out-of-band) before the orchestrator finished implementing.
    # Finalize to `done` here rather than spinning the dev session against
    # a branch that already landed.
    if _wf._finalize_if_pr_merged(gh, spec, issue, state):
        return

    # Closed-issue counterpart: the closed-`implementing` sweep yields
    # issues a human closed without a merged PR (rejected outright,
    # closed mid-implementation, or closed alongside a closed-without-
    # merge PR). Flip to `rejected` so the dev agent is not spawned
    # against a closed issue.
    if _wf._finalize_if_issue_closed(gh, spec, issue, state):
        return

    # Stale question-stage park: the operator relabeled from `question`
    # to `implementing`. `_handle_question` parks with
    # `awaiting_human=True` and `park_reason="question_*"` so its own
    # next tick can resume the locked question-agent session; those
    # flags are opaque to implementing's resume path and would
    # mis-fire below.
    #
    # The clear must check the actual worktree, NOT just the park
    # reason. The question agent is supposed to be read-only, but a
    # misbehaving run can park as `question_commits` / `question_dirty`
    # (or `question_timeout` that committed before being killed) with
    # unreviewed code state on the per-issue branch. Silently dropping
    # the park would let the fresh-spawn branch's recovered-worktree
    # shortcut (`_has_new_commits` -> push) publish the question
    # agent's commits as if a dev session had authored them, violating
    # the read-only contract.
    #
    # Two outcomes:
    #   * Worktree and local branch both clean -> the relabel IS the
    #     unblock signal: drop the question-stage park flags, ratchet
    #     `last_action_comment_id` past the question agent's answer
    #     comment so the eventual validating->in_review watermark seed
    #     cannot replay it as fresh PR feedback, and fall through to
    #     the fresh-spawn path.
    #   * Worktree carries dirty edits OR the local
    #     `orchestrator/<slug>/issue-N` branch carries commits beyond
    #     `origin/<base>` -> refuse to proceed. The branch check
    #     covers the case where the worktree was removed
    #     (`_cleanup_question_worktree` ran on a safe park, or the
    #     operator manually deleted the worktree dir) but the local
    #     branch survived with question-agent commits: without it,
    #     `_ensure_worktree` would silently restore the branch in a
    #     fresh worktree, `_has_new_commits` would return True in the
    #     recovered-worktree shortcut below, and those commits would
    #     ship as a dev PR. Re-park with `question_unsafe_relabel`
    #     and tell the operator to reset the branch (or delete it) so
    #     the dev agent can start from a clean base. The re-park is
    #     idempotent: once `park_reason` is already
    #     `question_unsafe_relabel`, subsequent ticks stay silent
    #     until the state is cleaned (which makes the clean branch
    #     fire) or the operator relabels elsewhere.
    park_reason = state.get("park_reason")
    if (
        state.get("awaiting_human")
        and isinstance(park_reason, str)
        and park_reason.startswith("question_")
    ):
        wt = _wf._worktree_path(spec, issue.number)
        worktree_dirty = wt.exists() and bool(
            _wf._worktree_dirty_files(wt)
        )
        unpushed_branch = _wf._branch_has_unpushed_commits(
            spec, issue.number,
        )
        if worktree_dirty or unpushed_branch:
            if park_reason != "question_unsafe_relabel":
                # Name the actual offending branch so the cleanup
                # hint (`git branch -D <name>`) targets it; a
                # legacy `orchestrator/issue-N` ref from a pre-
                # slug-namespacing park would otherwise be missed
                # if we only printed the resolved (namespaced)
                # name.
                branch_for_hint = (
                    unpushed_branch
                    or _wf._resolve_branch_name(state, spec, issue.number)
                )
                trigger = (
                    "dirty edits in the per-issue worktree"
                    if worktree_dirty and not unpushed_branch
                    else (
                        "unreviewed commits on the per-issue "
                        f"branch `{branch_for_hint}`"
                        if unpushed_branch and not worktree_dirty
                        else (
                            "unreviewed commits on the per-issue "
                            f"branch `{branch_for_hint}` "
                            "AND dirty edits in its worktree"
                        )
                    )
                )
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} relabeled to `implementing`, "
                    f"but the prior question-stage park "
                    f"(`{park_reason}`) left {trigger}. The question "
                    "agent must be read-only, so the orchestrator "
                    "refuses to push that work as a dev "
                    "implementation. Reset the worktree (e.g. "
                    "`git -C <worktree> reset --hard origin/<base> && "
                    "git -C <worktree> clean -fd`), or delete the "
                    f"local branch (`git branch -D "
                    f"{branch_for_hint}` in "
                    "`target_root`), before re-relabeling so the dev "
                    "agent starts from a clean base.",
                    reason="question_unsafe_relabel",
                )
                state.set("park_reason", "question_unsafe_relabel")
            gh.write_pinned_state(issue, state)
            return
        state.set("awaiting_human", False)
        state.set("park_reason", None)
        latest = gh.latest_comment_id(issue)
        if isinstance(latest, int):
            prior = state.get("last_action_comment_id")
            if not isinstance(prior, int) or latest > prior:
                state.set("last_action_comment_id", latest)

    # User-content drift: a human edited the issue title/body after the dev
    # session was spawned. The issue spec ("don't re-decompose mid-
    # implementation -- too disruptive") rules out routing back to
    # `decomposing` here; instead notify the human and resume the locked
    # dev session with the new body so it can decide what to do. When no
    # dev session exists yet (fresh `ready` -> `implementing` bounce that
    # hasn't spawned), just persist the new hash and let the fresh-spawn
    # branch below pick the new body up via `_build_implement_prompt`.
    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        state.set("user_content_hash", new_hash)
        # "Has a dev session ever spawned" is keyed off the persisted
        # role identity (`dev_agent`, or the legacy `codex_session_id`),
        # NOT off `dev_session_id` alone -- a first spawn whose
        # subprocess returned no session id (CLI hiccup, missing output
        # file) still recorded `dev_agent` and is a valid resume target.
        # `_resume_dev_with_text` handles `dev_sid=None` by spawning
        # fresh against the recorded spec, which is exactly what we
        # want here (the recorded spec also survives a config flip
        # between ticks).
        has_dev_session = bool(
            state.get("dev_agent") or state.get("codex_session_id")
        )
        if has_dev_session:
            _wf._post_issue_comment(
                gh, issue, state,
                ":pencil2: issue body changed; resuming dev session with "
                "the updated requirements.",
            )
            # Mark every issue-thread comment visible right now as
            # consumed: the dev session sees the full conversation via
            # `_recent_comments_text` in the resume prompt, so the next
            # validating->in_review handoff (via
            # `_seed_watermark_past_self`) must NOT replay those comments
            # as fresh PR feedback and re-resume the dev on input it has
            # already handled.
            _wf._mark_drift_comments_consumed(gh, issue, state)
            wt = _wf._worktree_path(spec, issue.number)
            if not wt.exists():
                wt = _wf._ensure_worktree(
                    spec, issue.number,
                    branch=_wf._resolve_branch_name(state, spec, issue.number),
                )
            # Snapshot HEAD BEFORE the resume so the post-result check
            # below can tell whether THIS resume produced a new commit.
            # `_has_new_commits` only compares against `origin/<base>`,
            # so a recovered worktree carrying pre-existing unpushed
            # commits from a previous tick would mask an empty / failed
            # resume here: an empty dev response would still walk into
            # `_on_commits` and open a PR against commits that never
            # got a chance to address the edited requirements.
            before_sha = _wf._head_sha(wt)
            followup = _wf._build_user_content_change_prompt(
                issue, _wf._recent_comments_text(issue),
            )
            wt, result = _resume_dev_with_text(
                gh, spec, issue, state, followup,
            )
            state.set("last_agent_action_at", _wf._now_iso())
            state.set("branch", _wf._resolve_branch_name(state, spec, issue.number))
            after_sha = _wf._head_sha(wt)
            this_resume_committed = (
                bool(after_sha) and after_sha != before_sha
            )
            if result.timed_out:
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} agent timed out after "
                    f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
                    reason="agent_timeout",
                )
            elif this_resume_committed:
                dirty = _wf._worktree_dirty_files(wt)
                if dirty:
                    _on_dirty_worktree(gh, issue, state, result, dirty)
                else:
                    _on_commits(gh, spec, issue, state, result)
            else:
                # The dev produced no new commit on THIS resume. Accept
                # it as an acknowledgement ONLY when the message ends
                # with the explicit `ACK: <reason>` marker emitted by
                # `_build_user_content_change_prompt`. Any other
                # no-commit response (a real clarification question, an
                # ambiguous comment, or an empty message) falls back to
                # `_on_question` so the issue parks awaiting human --
                # treating a clarification as an ack would post a
                # misleading "existing work satisfies" comment AND
                # leave `awaiting_human=False`, stranding the real
                # question. Recovered pre-existing commits from a prior
                # tick are deliberately NOT pushed here either: the dev
                # must explicitly commit again (or ACK) for the
                # orchestrator to treat the body change as handled.
                ack_reason = _wf._drift_ack_reason(result.last_message or "")
                if ack_reason:
                    quoted = "> " + ack_reason.replace("\n", "\n> ")
                    _wf._post_issue_comment(
                        gh, issue, state,
                        ":speech_balloon: dev session reports the existing "
                        f"work satisfies the edit:\n\n{quoted}",
                    )
                    state.set("silent_park_count", 0)
                else:
                    _on_question(gh, issue, state, result)
            gh.write_pinned_state(issue, state)
            return
        # No dev session yet. If the worktree carries recovered unpushed
        # commits from a previous tick, those commits were authored
        # BEFORE the human edited the issue and no agent has seen the
        # new body. Falling through would let the recovered-worktree
        # shortcut below push them and open a PR against requirements
        # the agent never read. Park awaiting human so the operator
        # decides whether to discard the recovered work and start over
        # or accept it as-is by relabeling. Without this guard, an
        # orchestrator restart between commit and PR open followed by a
        # body edit would silently publish stale work.
        #
        # We rely on `_has_new_commits` alone, not a `Path.exists()`
        # pre-check, because `_has_new_commits` already returns False
        # when the worktree is absent (the underlying `git rev-list`
        # fails) -- and the fake worktree paths used by tests never
        # exist on disk, so an `exists()` gate would short-circuit the
        # park branch in the regression test below.
        wt = _wf._worktree_path(spec, issue.number)
        if _wf._has_new_commits(spec, wt):
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
            return
        # No recovered commits. If the issue is parked awaiting human
        # WITHOUT a recorded dev session (unusual but possible: a manual
        # relabel, or drift detected on a freshly-picked-up issue that
        # parked before its first spawn), the awaiting-human branch
        # below would route to `_resume_developer_on_human_reply`. Two
        # failure modes there:
        #   (a) no new comments -- returns None and the handler returns
        #       WITHOUT writing the new hash, looping the drift detection
        #       on every subsequent tick;
        #   (b) new comments -- `_resume_dev_with_text` fresh-spawns with
        #       ONLY the new-comments followup text as the prompt, never
        #       quoting the updated body that triggered the drift in the
        #       first place.
        # Clear the park flags here so the fresh-spawn branch below
        # fires this tick with the full implement prompt (which quotes
        # the current `issue.body` and the full conversation via
        # `_recent_comments_text`). Mark every visible issue-thread
        # comment as consumed so the validating->in_review handoff
        # doesn't later replay them as fresh PR feedback.
        if state.get("awaiting_human"):
            _wf._post_issue_comment(
                gh, issue, state,
                ":pencil2: issue content changed; clearing the park and "
                "spawning a fresh dev run against the updated "
                "requirements.",
            )
            _wf._mark_drift_comments_consumed(gh, issue, state)
            state.set("awaiting_human", False)
            state.set("park_reason", None)
        # Fall through to the fresh-spawn path, which builds the
        # implement prompt from the current `issue.body` so the new
        # requirements are picked up naturally.

    if state.get("awaiting_human"):
        resumed = _resume_developer_on_human_reply(gh, spec, issue, state)
        if resumed is None:
            return
        wt, result = resumed
    else:
        wt = _wf._ensure_worktree(
            spec, issue.number,
            branch=_wf._resolve_branch_name(state, spec, issue.number),
        )
        if _wf._has_new_commits(spec, wt):
            # Recovered worktree: the dev agent already committed on a
            # previous tick; skip a fresh run and go straight to push.
            _wf.log.info(
                "issue=#%d skipping agent; worktree already has commits",
                issue.number,
            )
            _, _, _, dev_sid = _read_dev_session(state)
            result = AgentResult(
                session_id=dev_sid,
                last_message="(orchestrator restart: pushing previously committed work)",
                exit_code=0,
                timed_out=False,
                stdout="",
                stderr="",
            )
        else:
            if not _check_and_increment_retry_budget(gh, issue, state):
                gh.write_pinned_state(issue, state)
                return
            dev_spec, dev_backend, dev_args, _ = _read_dev_session(state)
            # Persist the spec BEFORE the spawn so a backend hiccup
            # that produces commits without surfacing a session id (an
            # empty codex `-o` file, an unparseable claude JSONL line)
            # does not leave `dev_agent` unset. A later `DEV_AGENT` env
            # flip would otherwise retarget the next resume at a
            # backend that never ran on this issue. Storing the parsed
            # backend alone would also strip any configured CLI args
            # on subsequent resumes. `_read_dev_session` already chose
            # `dev_spec` -- the current stored value when re-entering,
            # else `config.DEV_AGENT_SPEC` for a first-ever spawn --
            # so this is a no-op when state already carries the spec.
            state.set("dev_agent", dev_spec)
            prompt = _wf._build_implement_prompt(issue, _wf._recent_comments_text(issue))
            result = _wf._run_agent_tracked(
                gh, issue.number,
                agent_role="developer",
                stage="implementing",
                backend=dev_backend,
                prompt=prompt,
                cwd=wt,
                agent_spec=dev_spec,
                extra_args=dev_args,
                review_round=state.get("review_round", 0),
                retry_count=state.get("retry_count"),
            )
            if result.session_id:
                state.set("dev_session_id", result.session_id)
                # Fresh session -> its resume budget starts from zero, even
                # when a prior (retried) session left a non-zero count.
                state.set("dev_resume_count", 0)
        state.set("branch", _wf._resolve_branch_name(state, spec, issue.number))

    state.set("last_agent_action_at", _wf._now_iso())

    if result.timed_out:
        # Park on awaiting_human so the next tick doesn't restart codex or
        # push partial commits left in the worktree. The HITL reply acts as
        # the unblock signal, identical to the question path.
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent timed out after {config.AGENT_TIMEOUT}s, "
            "manual intervention needed.",
            reason="agent_timeout",
        )
        gh.write_pinned_state(issue, state)
        return

    wt = _wf._worktree_path(spec, issue.number)
    if _wf._has_new_commits(spec, wt):
        dirty = _wf._worktree_dirty_files(wt)
        if dirty:
            _on_dirty_worktree(gh, issue, state, result, dirty)
        else:
            _on_commits(gh, spec, issue, state, result)
    else:
        _on_question(gh, issue, state, result)

    gh.write_pinned_state(issue, state)


def _on_commits(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    result: AgentResult,
) -> None:
    from .. import workflow as _wf

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
    # Recover gracefully if a previous tick crashed between open_pr and the
    # relabel: reuse the existing open PR instead of 422-ing on duplicate.
    pr = gh.find_open_pr(branch=branch, base=spec.base_branch)
    if pr is None:
        title = _wf._pr_title_from_commit_or_issue(issue, _wf._first_commit_subject(spec, wt))
        _, dev_backend, _, dev_sid = _read_dev_session(state)
        body_parts = [
            f"Resolves #{issue.number}",
            "",
            f"Generated by orchestrator ({dev_backend} session `{dev_sid or '?'}`).",
        ]
        if result.last_message.strip():
            body_parts += ["", "---", "_Last agent message:_", "", result.last_message[:2000]]
        pr = gh.open_pr(
            branch=branch, base=spec.base_branch, title=title, body="\n".join(body_parts)
        )
        _wf._post_issue_comment(gh, issue, state, f":sparkles: PR opened: #{pr.number}")
        gh.emit_event(
            "pr_opened",
            issue_number=issue.number,
            stage="implementing",
            pr_number=pr.number,
            branch=branch,
            sha=getattr(pr.head, "sha", None) or None,
            retry_count=state.get("retry_count"),
        )
    else:
        _wf.log.info("issue=#%s reusing existing PR #%d for %s", issue.number, pr.number, branch)
    state.set("pr_number", pr.number)
    # Persist the pushed branch alongside `pr_number` so the next
    # tick's `_resolve_branch_name` can recover it directly. Without
    # this, a state that lacked `branch` going in (e.g. an
    # awaiting-human resume that opened the PR here without first
    # passing through the fresh-spawn branch-persist site) would
    # leave `pr_number` set with `branch` unset; the legacy-PR
    # fallback in `_resolve_branch_name` would then misroute every
    # downstream tick to `orchestrator/issue-<n>` while the live PR
    # is on the slug-namespaced branch this push just published.
    state.set("branch", branch)
    # Reset the review counter every time we (re-)open a PR so the validating
    # handler starts fresh on the new branch state.
    state.set("review_round", 0)
    # Issue moved forward; reset the implementing retry budget so any future
    # bounce back into implementing (e.g. validating -> implementing in a
    # later stage) starts with a fresh window.
    state.set("retry_count", 0)
    state.set("retry_window_start", None)
    # The session just produced commits, so it isn't poisoned -- reset the
    # silent-park streak so a future blip doesn't tip an otherwise-healthy
    # session past the fresh-session threshold.
    state.set("silent_park_count", 0)
    # Hand off straight to `validating`. The docs pass runs only as the
    # final-docs handoff after the reviewer agent approves.
    gh.set_workflow_label(issue, WorkflowLabel.VALIDATING)


def _on_question(
    gh: GitHubClient, issue: Issue, state: PinnedState, result: AgentResult
) -> None:
    from .. import workflow as _wf

    raw = result.last_message.strip()
    if raw:
        quoted = "> " + raw.replace("\n", "\n> ")
        _wf._post_issue_comment(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent needs your input to proceed:\n\n{quoted}",
        )
        state.set("awaiting_human", True)
        # Real question parks are not transient: they need a human reply
        # before the in_review ready-ping gates should run again. Clear
        # any stale `park_reason` left behind by a prior in_review
        # unmergeable park, and reset the silent-park streak.
        state.set("park_reason", None)
        state.set("silent_park_count", 0)
        park_reason = "agent_question"
    else:
        # No commits AND no final message -- the agent produced literally
        # nothing. Callers only invoke `_on_question` when the worktree has
        # no new commits, so an empty `last_message` here is a silent
        # failure, not a content question. The most common cause is a
        # poisoned resume of a session previously killed mid-stream (e.g.
        # by a Claude rate limit). Tag the park with a distinct reason so
        # `_resume_dev_with_text` can drop the dev session id after enough
        # consecutive silent parks, and surface the situation accurately
        # to the operator instead of impersonating a real "agent has a
        # question" park.
        diag = _wf._format_stderr_diagnostics(result, "Agent")
        _wf._post_issue_comment(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent produced no output (likely a "
            f"session-resume failure); manual intervention needed.{diag}",
        )
        _wf.log.warning(
            "issue=#%s agent produced no output; exit_code=%d "
            "timed_out=%s stderr_tail=%r",
            issue.number, result.exit_code, result.timed_out,
            _wf._stderr_log_tail(result),
        )
        state.set("awaiting_human", True)
        state.set("park_reason", "agent_silent")
        state.set(
            "silent_park_count",
            int(state.get("silent_park_count") or 0) + 1,
        )
        park_reason = "agent_silent"
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)
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
    result: AgentResult,
    dirty: list[str],
) -> None:
    """Park instead of pushing when the agent left uncommitted changes.

    Pushing here would publish a branch that omits the dirty files, so the PR
    would not match what the agent actually produced. We surface the situation
    to the human and resume the codex session on their reply, identical to the
    question path.
    """
    from .. import workflow as _wf

    shown = dirty[:10]
    files_md = "\n".join(f"- `{p}`" for p in shown)
    if len(dirty) > len(shown):
        files_md += f"\n- … ({len(dirty) - len(shown)} more)"
    last_msg = result.last_message.strip()
    tail = ""
    if last_msg:
        quoted = "> " + last_msg.replace("\n", "\n> ")
        tail = f"\n\n_Last agent message:_\n\n{quoted}"
    _wf._post_issue_comment(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent committed but left {len(dirty)} "
        f"uncommitted change(s); refusing to push an incomplete branch. "
        f"Reply with guidance and the orchestrator will resume the session.\n\n"
        f"{files_md}{tail}",
    )
    state.set("awaiting_human", True)
    # Mirror `_on_question`: not transient, clear any stale `park_reason`
    # so a prior transient in_review park does not auto-recover over the
    # standing dirty-worktree question. Clear the silent-park streak too:
    # the agent produced output, so the session is not poisoned.
    state.set("park_reason", None)
    state.set("silent_park_count", 0)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)
    gh.emit_event(
        "park_awaiting_human",
        issue_number=issue.number,
        stage=gh.workflow_label(issue),
        reason="dirty_worktree",
        dirty_files=len(dirty),
    )
