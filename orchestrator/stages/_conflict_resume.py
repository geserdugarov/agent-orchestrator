# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Conflict resume."""
from __future__ import annotations

from orchestrator.stages import _conflict_state as _state
from orchestrator.stages import conflicts as _owner

_ConflictContext = _owner._ConflictContext
_ConflictResumeRun = _owner._ConflictResumeRun
Optional = _owner.Optional
filter_trusted = _owner.filter_trusted
_CONFLICT_ROUND = _state._CONFLICT_ROUND


def _resume_on_user_content_change(
    ctx: _ConflictContext,
    pr_number,
    new_hash: str,
) -> None:
    """Resume the dev session after a human edited the issue body mid-rebase.

    Posts a resuming ack, marks the drift comments consumed, and resumes
    the dev on the updated body+comments. On a pushed fix bumps the
    conflict round and hands to `validating`; on an ack (no commit) stays
    in `resolving_conflict` without parking. The caller returns immediately
    after this helper runs. Persists pinned state on every exit EXCEPT the
    shutdown-sweep-interrupted / live-paused short-circuits, which return
    without writing so the drift stays unconsumed and re-runs next process.
    """
    from orchestrator import workflow as _wf

    ctx.state.set("user_content_hash", new_hash)
    _wf._post_pr_comment(
        ctx.gh, int(pr_number), ctx.state,
        ":pencil2: issue body changed; resuming dev session.",
    )
    # Mark issue-thread comments as consumed: the dev sees the full thread via
    # `_recent_comments_text`, and the eventual validating->in_review handoff
    # (after a successful pushed resolution flips back to validating) must not
    # replay them.
    _wf._mark_drift_comments_consumed(ctx.gh, ctx.issue, ctx.state)
    wt = _owner._ensure_conflict_worktree(ctx)
    before_sha = _wf._head_sha(wt)
    followup = _wf._build_user_content_change_prompt(
        ctx.issue, _wf._recent_comments_text(ctx.issue),
    )
    run = _owner._run_conflict_resume(ctx, followup)
    # Shutdown-sweep interruption: ignore the partial result and return WITHOUT
    # writing pinned state -- the drift bookkeeping (refreshed
    # `user_content_hash`, consumed comments, session mutations) above is
    # discarded so the next process re-detects and re-runs the drift resume.
    # Must precede `_post_user_content_change_result`, which has no interrupted
    # check of its own and would otherwise parse `last_message` / route through
    # `_on_question` before the caller persists those changes.
    if _wf._ignore_if_interrupted(ctx.issue, run.dev_result):
        return
    # Live pause applied mid-run: an operator added `paused` (or `backlog`)
    # while this drift resume was in flight. Same short-circuit as the
    # interrupted branch -- return before `_post_user_content_change_result`,
    # the conflict-round bump, or any relabel / pinned-state write, so the
    # drift stays unconsumed and the committed work stays on the branch until
    # the label is removed.
    if run.paused:
        return
    outcome = _wf._post_user_content_change_result(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, run.worktree,
        run.dev_result, before_sha,
    )
    if outcome == "pushed":
        # Pushed branch diff -> hand straight back to validating; the single
        # docs pass runs after final reviewer approval.
        _owner._hand_resolved_round_to_validating(
            ctx, int(ctx.state.get(_CONFLICT_ROUND) or 0), pr_number,
            outcome="drift_resolved", sha=_wf._head_sha(run.worktree),
        )
        return
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _resume_awaiting_human(
    ctx: _ConflictContext, conflict_round: int,
) -> None:
    """Resume a parked rebase on a fresh human reply.

    Collects comments past `last_action_comment_id`, resumes the dev with
    their text, and funnels the result through
    `_post_conflict_resolution_result`. Returns without writing pinned
    state when no reply has arrived yet or a live pause landed mid-run; on
    a real reply the shared funnel owns the push / relabel / state write.
    """
    from orchestrator import workflow as _wf

    followup = _owner._awaiting_human_followup(ctx)
    if followup is None:
        return
    wt = _owner._ensure_conflict_worktree(ctx)
    before_sha = _wf._head_sha(wt)
    run = _owner._run_conflict_resume(ctx, followup)
    # Live pause applied mid-run: honor the helper's decision and return
    # before `_post_conflict_resolution_result` (which parses the result,
    # pushes, relabels, and writes pinned state). The in-progress rebase stays
    # on the branch until the label is removed.
    if run.paused:
        return
    # No explicit lease here: resume worktrees may be mid-rebase or ahead of
    # the remote PR head, so `before_sha` is not necessarily the remote SHA.
    # Let `_push_branch` lease against live ls-remote.
    _owner._post_conflict_resolution_result(ctx, run, before_sha, conflict_round)


def _awaiting_human_followup(ctx: _ConflictContext) -> Optional[str]:
    """Build the dev-resume prompt for a parked rebase from the trusted human
    reply, or return ``None`` when the tick is handled without a resume.

    Returns ``None`` when no trusted reply has arrived yet (no state write) or
    the `/orchestrator continue` command is refused (park written). Otherwise
    advances the consumed-comment watermark and returns the retry prompt or the
    joined reply text.
    """
    from orchestrator import workflow as _wf

    last_action_id = ctx.state.get("last_action_comment_id")
    # Drop untrusted authors up front (mirrors `_resume_developer_on_human_reply`):
    # with `ALLOWED_ISSUE_AUTHORS` set an outsider reply on a parked rebase must
    # not steer the developer NOR advance the consumed watermark. Only trusted
    # comments are consumed, so an outsider reply trailing a trusted one is left
    # unconsumed; an all-untrusted batch is treated as "no human reply yet".
    new_comments = filter_trusted(ctx.gh.comments_after(ctx.issue, last_action_id))
    if not new_comments:
        return None  # no human reply yet
    # `/orchestrator continue` on a parked rebase, BEFORE the generic comment
    # resume. A session-failure park (`agent_silent` / `agent_timeout`) retries
    # the dev intentionally on a neutral prompt -- NOT the literal command,
    # which the dev has no context for -- while a park needing a real answer
    # refuses. Auto-rebase parks belong to the refresh retry-unpark, so leave
    # those (and command-plus-guidance / normal replies) to the resume below.
    park_reason = ctx.state.get("park_reason")
    continue_action = (
        "passthrough" if park_reason in _wf._AUTO_REBASE_PARK_REASONS
        else _wf._continue_command_action(new_comments, park_reason)
    )
    if continue_action == "refuse":
        _wf._refuse_parked_continue(ctx.gh, ctx.issue, ctx.state)
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)
        return None
    ctx.state.set(
        "last_action_comment_id", max(comment.id for comment in new_comments),
    )
    if continue_action == "retry":
        return f"{_wf._CONTINUE_RETRY_PROMPT}\n\n{_wf._FOREGROUND_ONLY_NOTE}"
    joined = "\n\n".join(
        _wf._quote_comment_line(comment)
        for comment in new_comments
        if comment.body
    )
    return f"{joined}\n\n{_wf._FOREGROUND_ONLY_NOTE}"


def _run_conflict_resume(
    ctx: _ConflictContext, followup: str,
) -> _ConflictResumeRun:
    """Resume the locked dev session over `followup` and stamp the agent
    action time. Shared by the drift, awaiting-human, and fresh-conflict
    resume paths."""
    from orchestrator import workflow as _wf

    wt, conflict_result, paused = _wf._resume_dev_with_text(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, followup, pause_guard=True,
    )
    ctx.state.set("last_agent_action_at", _wf._now_iso())
    return _ConflictResumeRun(worktree=wt, dev_result=conflict_result, paused=paused)
