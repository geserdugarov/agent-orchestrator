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

from typing import Optional, Tuple

from github.Issue import Issue

from .. import config
from ..agents import AgentResult
from ..config import RepoSpec
from ..state_machine import WorkflowLabel
from ..github import GitHubClient, PinnedState


# Park reasons whose underlying condition keeps the per-issue
# worktree on disk for human inspection. The `_handle_question`
# finally block reads `state.park_reason` against this set so a
# no-reply tick that returns early via the awaiting-human branch
# does NOT tear down the worktree the prior tick explicitly left
# for the operator to inspect.
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
_UNSAFE_QUESTION_PARKS = frozenset({
    "question_timeout", "question_commits", "question_dirty",
})


def _read_question_session(
    state: PinnedState,
) -> Tuple[str, str, tuple[str, ...], Optional[str]]:
    """Return (spec, backend, extra_args, question_session_id) for an issue.

    Mirrors `_read_dev_session` / `_read_decomposer_session`: `spec` is
    the full configured command string the next run will use. Callers
    persist it verbatim BEFORE invoking `run_agent` so a fresh spawn
    that yields no `session_id` (CLI hiccup, empty `-o` file) still
    records the role identity and a later `DECOMPOSE_AGENT` env flip
    cannot retarget the next awaiting-human resume at a different
    backend.

    Legacy bare-backend values (`"codex"` / `"claude"`) round-trip
    cleanly to `(backend, ())`. When the issue has never spawned a
    question agent, returns the current config's
    `(DECOMPOSE_AGENT_SPEC, DECOMPOSE_AGENT, DECOMPOSE_AGENT_ARGS, None)`.
    """
    stored = state.get("question_agent")
    if stored:
        spec = str(stored)
        backend, args = config._parse_agent_spec("question_agent", spec)
        sid = state.get("question_session_id")
        return spec, backend, args, str(sid) if sid is not None else None
    return (
        config.DECOMPOSE_AGENT_SPEC,
        config.DECOMPOSE_AGENT,
        config.DECOMPOSE_AGENT_ARGS,
        None,
    )


def _resume_question_on_human_reply(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState
) -> Optional[AgentResult]:
    """Resume the question session with new issue-thread comments.

    Returns the AgentResult, or None if no new comments arrived since
    the last park (caller should return without writing state). Mirrors
    `_resume_developer_on_human_reply` -- the watermark advances BEFORE
    the spawn so a crashed/timed-out resume still records the comments
    as consumed (the agent did see them via the followup prompt).
    """
    from .. import workflow as _wf

    last_action_id = state.get("last_action_comment_id")
    new_comments = gh.comments_after(issue, last_action_id)
    if not new_comments:
        return None
    consumed_max = max(c.id for c in new_comments)
    state.set("last_action_comment_id", consumed_max)
    wt = _wf._worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _wf._ensure_worktree(
            spec, issue.number,
            branch=_wf._resolve_branch_name(state, spec, issue.number),
        )
    question_spec, question_backend, question_args, question_sid = (
        _read_question_session(state)
    )
    # When we have a live session to resume, the brief follow-up
    # prompt is enough -- the agent already has the issue body /
    # title / prior conversation cached in its session state.
    # Without a session id (the prior tick's CLI hiccup left
    # `question_session_id` empty), `_run_agent_tracked` starts a
    # fresh agent that has no cached context, so a followup-only
    # prompt would arrive without an issue body, title, or prior
    # conversation and the agent would have nothing to answer
    # against. Switch to the full question prompt in that case so
    # the recovery spawn sees the same context a first-tick run
    # would, with the human's reply visible in the conversation
    # block via `_recent_comments_text`.
    if question_sid is None:
        prompt = _wf._build_question_prompt(
            issue, _wf._recent_comments_text(issue),
        )
    else:
        prompt = _wf._build_question_followup_prompt(new_comments)
    result = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="question",
        stage="question",
        backend=question_backend,
        prompt=prompt,
        cwd=wt,
        agent_spec=question_spec,
        resume_session_id=question_sid,
        extra_args=question_args,
    )
    # Persist the (possibly new) session id from this resume too. A
    # prior tick that yielded no session id (CLI hiccup) would
    # otherwise leave `question_session_id` empty forever and every
    # future resume would re-spawn fresh instead of continuing the
    # locked conversation. Mirrors the fresh-spawn persistence in
    # `_handle_question`.
    if result.session_id:
        state.set("question_session_id", result.session_id)
    state.set("awaiting_human", False)
    return result


def _park_question(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    message: str,
    *,
    reason: str,
) -> None:
    """Park the issue awaiting human and emit the `park_awaiting_human`
    audit event with the question-stage reason tag.

    Wraps `_park_awaiting_human` so every question-stage park funnels
    through one place and the persistent `park_reason` field is
    re-set after the helper clears it (callers that need a transient
    reason re-set it themselves -- see the `_park_awaiting_human`
    docstring).
    """
    from .. import workflow as _wf

    _wf._park_awaiting_human(gh, issue, state, message, reason=reason)
    state.set("park_reason", reason)


def _handle_question(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)

    # Human closed the Q&A thread: that's the terminal signal. Do NOT
    # spawn the agent (the question is moot once the issue is closed),
    # stamp terminal state, flip the workflow label to `done`, and tear
    # down the per-issue worktree + local branch. `list_pollable_issues`
    # is what surfaces a closed `question` issue here in the first place;
    # once we flip the label to `done`, the closed-issue sweep no longer
    # yields it and the tick cost stays bounded in steady state. Even
    # an unsafe park's preserved worktree is reaped here -- by closing
    # the issue the operator has signaled they're done with it, so the
    # inspection window ends.
    if getattr(issue, "state", "open") == "closed":
        state.set("question_closed_at", _wf._now_iso())
        gh.set_workflow_label(issue, WorkflowLabel.DONE)
        gh.write_pinned_state(issue, state)
        _wf._cleanup_question_worktree(
            spec, issue.number,
            branch=_wf._resolve_branch_name(state, spec, issue.number),
        )
        return

    # Tracks whether to KEEP the per-issue worktree past this tick.
    # The question stage is read-only, so the default is to tear it
    # down (see `_cleanup_question_worktree` for the leak this
    # closes). Only the three unsafe-park branches below set
    # `keep_worktree = True` so the operator can inspect what the
    # misbehaving agent did before resetting:
    #   * `question_commits` -- the agent committed.
    #   * `question_dirty`   -- the agent left uncommitted edits.
    #   * `question_timeout` -- the agent was killed mid-run and may
    #                            have left either of the above.
    # The defensive `_sync_worktree_with_base` label skip then
    # prevents the per-tick base refresh from merging
    # `origin/<base>` over that kept inspection state.
    #
    # Seeded from `state.park_reason` so a no-reply tick on a prior
    # unsafe park ALSO preserves the worktree: without this, the
    # awaiting-human early return below would let the `finally`
    # tear down the inspection target on every subsequent tick.
    # The safe / answer branches override this initial value
    # explicitly so an operator who resets the worktree and replies
    # cleanly (resume produces a clean answer with no new commits /
    # dirty) ends the inspection window normally.
    keep_worktree = state.get("park_reason") in _UNSAFE_QUESTION_PARKS
    try:
        # Awaiting-human resume: the prior tick posted an answer /
        # follow-up and parked. If the human has replied since,
        # resume the locked session with their text. If they have
        # not, this tick is a no-op -- but we still let the finally
        # block tear down any worktree left from a prior tick so
        # the base refresh has nothing to merge into.
        if state.get("awaiting_human"):
            resumed = _resume_question_on_human_reply(
                gh, spec, issue, state,
            )
            if resumed is None:
                return
            result = resumed
            wt = _wf._worktree_path(spec, issue.number)
        else:
            wt = _wf._ensure_worktree(
                spec, issue.number,
                branch=_wf._resolve_branch_name(state, spec, issue.number),
            )
            question_spec, question_backend, question_args, _ = (
                _read_question_session(state)
            )
            # Persist the spec BEFORE the spawn so a backend hiccup
            # that yields no session id (empty codex `-o` file,
            # unparseable claude JSONL line) still leaves a durable
            # role-identity record. A later `DECOMPOSE_AGENT` env
            # flip otherwise retargets the next resume at a backend
            # that never ran on this issue. Storing the parsed
            # backend alone would also strip configured CLI args
            # from subsequent resumes.
            state.set("question_agent", question_spec)
            prompt = _wf._build_question_prompt(
                issue, _wf._recent_comments_text(issue),
            )
            result = _wf._run_agent_tracked(
                gh, issue.number,
                agent_role="question",
                stage="question",
                backend=question_backend,
                prompt=prompt,
                cwd=wt,
                agent_spec=question_spec,
                extra_args=question_args,
            )
            if result.session_id:
                state.set("question_session_id", result.session_id)

        state.set("last_question_at", _wf._now_iso())

        if result.timed_out:
            # Keep the worktree: the timeout killed the agent mid-run
            # and it may have committed or left dirty edits before
            # being killed. The operator inspects, then resets.
            keep_worktree = True
            _park_question(
                gh, issue, state,
                f"{config.HITL_MENTIONS} question agent timed out "
                f"after {config.AGENT_TIMEOUT}s; manual intervention "
                "needed. The per-issue worktree is left intact for "
                "inspection.",
                reason="question_timeout",
            )
            gh.write_pinned_state(issue, state)
            return

        # The question agent must be read-only. Commits or a dirty
        # index are misbehavior -- park with the worktree intact so
        # the operator can inspect what the agent did (mirrors the
        # decomposer's dirty park, which also keeps its worktree
        # past the tick).
        if _wf._has_new_commits(spec, wt):
            keep_worktree = True
            _park_question(
                gh, issue, state,
                f"{config.HITL_MENTIONS} question agent committed in "
                "the worktree but this stage is read-only; refusing "
                "to push. Reset the worktree before resuming.",
                reason="question_commits",
            )
            gh.write_pinned_state(issue, state)
            return

        dirty = _wf._worktree_dirty_files(wt)
        if dirty:
            keep_worktree = True
            shown = dirty[:10]
            files_md = "\n".join(f"- `{p}`" for p in shown)
            if len(dirty) > len(shown):
                files_md += f"\n- ... ({len(dirty) - len(shown)} more)"
            _park_question(
                gh, issue, state,
                f"{config.HITL_MENTIONS} question agent left "
                f"{len(dirty)} uncommitted change(s) but this stage "
                "is read-only; refusing to push. Reset the worktree "
                f"before resuming.\n\n{files_md}",
                reason="question_dirty",
            )
            gh.write_pinned_state(issue, state)
            return

        raw = (result.last_message or "").strip()
        if not raw:
            # Fully silent run: no commit AND no final message. Same
            # diagnosis as the implementer's silent-failure park --
            # usually a poisoned resume of a session previously
            # killed mid-stream. Safe to clean up the worktree (we
            # just verified it is not dirty / committed above) -- a
            # prior unsafe park's preservation ends here because the
            # current worktree state is provably clean.
            keep_worktree = False
            diag = _wf._format_stderr_diagnostics(
                result, "Question agent",
            )
            _park_question(
                gh, issue, state,
                f"{config.HITL_MENTIONS} question agent produced no "
                "output (likely a session-resume failure); manual "
                f"intervention needed.{diag}",
                reason="question_silent",
            )
            _wf.log.warning(
                "issue=#%s question agent produced no output; "
                "exit_code=%d timed_out=%s stderr_tail=%r",
                issue.number, result.exit_code, result.timed_out,
                _wf._stderr_log_tail(result),
            )
            gh.write_pinned_state(issue, state)
            return

        # Happy path: post the agent's answer (or clarifying
        # follow-up) to the issue thread, pinging HITL_MENTIONS so
        # the human is notified, and park awaiting human. The
        # human's reply -- whether an answer, a relabel to
        # `implementing`, or a close -- is the unblock signal. The
        # worktree is torn down in the finally block so the next
        # tick's `_refresh_base_and_worktrees` has nothing to merge
        # into. The explicit clear here ends a prior unsafe park's
        # inspection window: the agent's resume produced a clean
        # answer with no new commits / dirty, so the worktree is
        # safe to reap (the operator either reset it before
        # replying, or the prior unsafe state was transient).
        keep_worktree = False
        quoted = "> " + raw.replace("\n", "\n> ")
        _park_question(
            gh, issue, state,
            f"{config.HITL_MENTIONS} question agent responded:\n\n"
            f"{quoted}",
            reason="question_answer",
        )
        gh.write_pinned_state(issue, state)
    finally:
        if not keep_worktree:
            _wf._cleanup_question_worktree(
                spec, issue.number,
                branch=_wf._resolve_branch_name(state, spec, issue.number),
            )
