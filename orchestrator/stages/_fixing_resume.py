# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fixing resume."""
from __future__ import annotations

from orchestrator.stages import _fixing_state as _state
from orchestrator.stages import fixing as _owner

_FixingContext = _owner._FixingContext
_FixingFeedback = _owner._FixingFeedback
_FixingResumeRun = _owner._FixingResumeRun
AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
datetime = _owner.datetime
timezone = _owner.timezone
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_PENDING_FIX_AT = _state._PENDING_FIX_AT
_REVIEW_ROUND = _state._REVIEW_ROUND


def _fixing_debounce_open(
    feedback: _FixingFeedback, replay_batch,
) -> bool:
    """True while the quiet window is still open: hold the resume until no
    comment has landed for `IN_REVIEW_DEBOUNCE_SECONDS`.

    A newer comment arriving on a later tick is naturally picked up by the
    rescan, which extends the wait because the freshest timestamp controls
    the gate. Comments without a usable timestamp (older fakes, PyGithub
    edge cases) do not block the resume; in production `created_at` /
    `submitted_at` are always set. An accepted `/orchestrator continue`
    (`replay_batch` set) skips the wait entirely -- it is a deliberate
    operator signal, not chatter to debounce.
    """
    from orchestrator import workflow as _wf

    if replay_batch is not None:
        return False
    now = datetime.now(timezone.utc)
    latest_ts: Optional[datetime] = None
    for feedback_item in feedback.all_items:
        ts = _wf._comment_created_at(feedback_item)
        if ts is None:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
    return (
        latest_ts is not None
        and (now - latest_ts).total_seconds() < config.IN_REVIEW_DEBOUNCE_SECONDS
    )


def _apply_fix_review_round(state, pending_fix_at_was_set: bool) -> None:
    """Update `review_round` on a pushed fix per the route discriminator.

      * in_review->fixing (`pending_fix_at` was set): reset to 0. The
        previous reviewer round was APPROVED (the in_review HITL ping is
        gated on approval); the new fix starts a fresh round-count so
        MAX_REVIEW_ROUNDS does not trip prematurely on issues that pass
        back through review after a human PR comment.
      * validating->fixing (a CHANGES_REQUESTED dev fix that parked and
        was finished via a human reply): bump. The previous round was
        CHANGES_REQUESTED, not APPROVED, so we are still in the same
        review cycle and the round counter must advance to keep
        MAX_REVIEW_ROUNDS accounting honest.
    """
    if pending_fix_at_was_set:
        state.set(_REVIEW_ROUND, 0)
    else:
        round_n = int(state.get(_REVIEW_ROUND) or 0)
        state.set(_REVIEW_ROUND, round_n + 1)


def _run_fixing_resume(
    ctx: _FixingContext, followup: str,
) -> _FixingResumeRun:
    """Ensure the worktree, resume the locked dev session over `followup`,
    refresh the user-content drift hash, and read HEAD before/after.

    The hash refresh includes any human issue-thread comments we just fed to
    the dev via `followup`. Without it, the next tick that runs
    `_handle_validating` (or any other handler that calls
    `_detect_user_content_change`) would see those consumed comments as fresh
    user-content drift and resume the dev a second time on input it has already
    handled. Mirrors the hash refresh `_handle_in_review` does at the moment it
    routes to `fixing`. Refresh on BOTH success and failure paths: the dev saw
    the comments via the prompt either way, so the baseline must move with the
    consumption regardless of whether the agent pushed a fix this tick.

    HEAD is read only when the run did not time out -- the timeout branch of
    `_handle_dev_fix_result` returns before it would use `after_sha`, and
    reading here would burn an extra `_head_sha` the timeout path never did.
    """
    from orchestrator import workflow as _wf

    wt = _wf._worktree_path(ctx.spec, ctx.issue.number)
    if not wt.exists():
        wt = _wf._ensure_worktree(
            ctx.spec, ctx.issue.number,
            branch=_wf._resolve_branch_name(ctx.state, ctx.spec, ctx.issue.number),
        )
    before_sha = _wf._head_sha(wt)
    wt, dev_result, paused = _wf._resume_dev_with_text(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, followup, pause_guard=True,
    )
    ctx.state.set("last_agent_action_at", _wf._now_iso())
    ctx.state.set(
        "user_content_hash",
        _wf._compute_user_content_hash(
            ctx.issue, _wf._orchestrator_ids(ctx.state),
        ),
    )
    after_sha = None if dev_result.timed_out else _wf._head_sha(wt)
    return _FixingResumeRun(
        worktree=wt,
        dev_result=dev_result,
        paused=paused,
        before_sha=before_sha,
        after_sha=after_sha,
    )


def _fixing_ack_fast_path(
    ctx: _FixingContext,
    wt: Path,
    feedback: _FixingFeedback,
    dev_result: AgentResult,
    after_sha: Optional[str],
) -> bool:
    """In_review-route ACK fast path. Returns True (and relabels to
    `in_review`) when the dev's no-commit reply carried an explicit
    `ACK: <reason>` marker vouching that the PR feedback needs no actionable
    change; False to fall through to `_handle_dev_fix_result`.

    A vague "continue" / "ok" nudge should not strand a complete, mergeable PR
    in `fixing`, so an ack returns to `in_review` (re-arming the ready-ping)
    instead of parking.

    The fast path stands down on the stranded-fix shape: the ack vouches for
    the *feedback*, not for the publish state, so when the clean HEAD is
    strictly ahead of the remote PR branch (a fix a prior parked run committed
    but never pushed -- e.g. a dirty-park whose stray files were later cleaned
    up) relabeling to `in_review` here would clear the bookmarks, advance the
    watermarks, and present a PR head that is still missing the committed fix.
    Falling through lets `_handle_dev_fix_result` publish the stranded HEAD
    through its normal push tail and the pushed-fix exit route the freshened
    head back to the reviewer. The stranded check is skipped when `after_sha`
    is unreadable (mirrors `_handle_dev_fix_result`'s own gate -- no pushing
    blind off a worktree whose HEAD we could not read).
    """
    from orchestrator import workflow as _wf

    ack_reason = _wf._drift_ack_reason(dev_result.last_message or "")
    if not ack_reason or (
        after_sha and _wf._stranded_fix_unpushed(ctx.spec, wt, ctx.state, ctx.issue)
    ):
        return False
    _owner._advance_consumed_watermarks(ctx.state, feedback)
    _owner._clear_pending_fix_bookmarks(ctx.state)
    quoted = _wf._as_blockquote(ack_reason)
    _wf._post_issue_comment(
        ctx.gh, ctx.issue, ctx.state,
        ":speech_balloon: dev session reports the PR feedback needs "
        f"no change:\n\n{quoted}\n\nReturning to `in_review`.",
    )
    # The session is alive and producing a coherent ack, so reset the
    # silent-park streak (mirrors the drift-ack handling).
    ctx.state.set("silent_park_count", 0)
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.IN_REVIEW)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)
    return True


def _resume_fixing_and_dispatch_result(
    ctx: _FixingContext,
    feedback: _FixingFeedback,
    replay_batch,
) -> None:
    """Resume the locked dev session over the unread feedback (or a preserved
    `/orchestrator continue` batch), then dispatch the result: the in_review-
    route ACK fast path, the pushed-fix bounce back to `validating`, or a park
    via `_handle_dev_fix_result`.

    Runs after the quiet window has elapsed. Owns the resume, the interrupted /
    live-paused guards, the consumed-watermark advance, and the route round
    bookkeeping.
    """
    from orchestrator import workflow as _wf

    # Capture the route discriminator BEFORE the bookmark-clear branches below.
    # `pending_fix_at` is untouched between the tick's capture point and here
    # (no reachable path clears it in between), and the pushed-fix tail clears
    # the bookmarks only after this read.
    pending_fix_at_was_set = ctx.state.get(_PENDING_FIX_AT) is not None

    # On an accepted `/orchestrator continue`, resume on the PRESERVED batch
    # (plus any new feedback that came with the command), not the command
    # text -- the whole point of the command is to not lose the review
    # feedback the parked session never addressed.
    followup = _wf._build_pr_comment_followup(
        feedback.all_items if replay_batch is None else replay_batch
    )
    run = _owner._run_fixing_resume(ctx, followup)

    # A shutdown-killed (interrupted) resume is ignored entirely: its partial
    # last_message is not a real ACK or question, and `_handle_dev_fix_result`
    # refuses to publish an interrupted run regardless of HEAD. Bail WITHOUT
    # persisting state -- the ACK fast path, the consumed-watermark advance,
    # and the write below never run, and the awaiting_human reset / hash
    # refresh staged earlier this tick are dropped because we skip
    # `write_pinned_state`. The next tick re-discovers the same comments
    # (watermarks unmoved, bookmarks intact, awaiting_human unchanged) and
    # re-feeds them to a fresh dev session. This MUST cover the new-commit
    # case too: a kill that had advanced HEAD would otherwise fall through to
    # `_handle_dev_fix_result` (returns False, no push) and the watermark
    # advance below would consume the feedback while the local commit sits
    # unpushed -- the next tick would then see no feedback and bounce a PR
    # head that is missing the fix. Leaving the commit on disk lets a later
    # clean run republish it via the stranded-fix tail.
    if run.dev_result.interrupted:
        return

    # Live pause applied while the agent ran: an operator added `paused` (or
    # `backlog`) mid-run. Honor the decision `_resume_dev_with_text` already
    # made (propagated, not re-fetched) and stop before the ACK fast path, the
    # stranded-fix publish, `_handle_dev_fix_result`, the watermark advance, or
    # any relabel / pinned-state write. The committed work stays on the branch,
    # so once the label is removed the normal recovered / stranded-fix path
    # republishes it.
    if run.paused:
        return

    # ACK fast path (in_review route only): the dev made no commit but
    # explicitly signaled via the `ACK: <reason>` marker that the PR feedback
    # carries no actionable change. The validating CHANGES_REQUESTED route
    # (`pending_fix_at` unset) is excluded -- the reviewer DID request a
    # concrete change, so an ACK there falls through to `_handle_dev_fix_result`,
    # which parks for the human unless its stranded-fix check publishes a
    # committed-but-unpushed fix instead (`validating._stranded_fix_unpushed`).
    if (
        pending_fix_at_was_set
        and not run.dev_result.timed_out
        and (not run.after_sha or run.after_sha == run.before_sha)
        and _owner._fixing_ack_fast_path(
            ctx, run.worktree, feedback, run.dev_result, run.after_sha,
        )
    ):
        return

    pushed = _wf._handle_dev_fix_result(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, run.worktree, run.dev_result,
        run.before_sha, after_sha=run.after_sha,
    )

    # Advance the three in_review watermarks ONLY to the max id actually fed to
    # the dev on each surface (ratcheted against the current watermark).
    # Deliberately tighter than `_bump_in_review_watermarks`, which also pulls
    # in `gh.latest_comment_id(issue)`: a human issue-thread comment that
    # landed AFTER `feedback` was built but BEFORE this write was never quoted
    # in the dev's `_build_pr_comment_followup` prompt, so silently moving the
    # watermark past it would swallow real feedback.
    #
    # This applies to BOTH paths:
    #
    #   * On a pushed fix, the next in_review tick (after `validating`
    #     completes) must rediscover the concurrent comment as fresh PR
    #     feedback.
    #
    #   * On park/failure (timeout / dirty / push fail / no-commit), the next
    #     fixing tick must also rediscover it -- otherwise the
    #     `awaiting_human and not new_feedback` gate fires and the concurrent
    #     human comment is silently dropped, breaking the "comments arriving
    #     while already labeled `fixing`" contract on every failure mode.
    #
    # The orchestrator's own park comment posted by `_park_awaiting_human`
    # (issue id-space, body carries `_ORCH_COMMENT_MARKER` and its id is
    # recorded in `orchestrator_comment_ids`) does NOT need a watermark bump to
    # avoid replay: the next tick's rescan filters by both id and body marker,
    # so the park comment is dropped even when the watermark sits below it.
    _owner._advance_consumed_watermarks(ctx.state, feedback)

    if not pushed:
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)
        return

    # Bookmarks served their purpose; clear them so a later in_review->fixing
    # route writes fresh values rather than mixing rounds.
    # `_apply_fix_review_round` then updates `review_round` per the route
    # discriminator (`pending_fix_at_was_set`), and we flip DIRECTLY to
    # `validating` so the reviewer re-evaluates the new head next tick. Docs do
    # not run on this exit -- the single docs pass is deferred to the final-docs
    # handoff after reviewer approval, so running the docs stage against an
    # unapproved diff here would just push a no-op and waste a tick.
    _owner._clear_pending_fix_bookmarks(ctx.state)
    _owner._apply_fix_review_round(ctx.state, pending_fix_at_was_set)
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _handle_fixing(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)

    pr = _owner._fixing_preflight(gh, spec, issue, state)
    if pr is None:
        return

    feedback = _owner._rescan_fixing_feedback(gh, issue, pr, state)

    # `replay_batch` is set only by an accepted `/orchestrator continue`
    # command inside `_dispatch_parked_fixing`: the PRESERVED PR-feedback batch
    # (plus any genuinely new feedback that arrived with the command) to resume
    # the fresh dev on, instead of the per-tick rescan. It skips the debounce
    # and re-grounds a dropped session in the resume tail.
    #
    # `_dispatch_parked_fixing` bails (`stop=True`) unless something new has
    # arrived since the park bump: the watermarks were advanced past the
    # previously-consumed feedback, so `feedback` can only carry genuinely new
    # content, and without that guard a single poisoned tick would loop on
    # every poll, spamming the same dev-resume prompt.
    replay_batch: Optional[list] = None
    if state.get(_AWAITING_HUMAN):
        parked = _owner._dispatch_parked_fixing(
            _FixingContext(gh, spec, issue, state, pr), feedback,
        )
        if parked.stop:
            return
        replay_batch = parked.replay_batch

    # Watermarks already cover the triggering bookmarks (a prior tick consumed
    # them, or an operator advanced them manually). Nothing left to address;
    # clear the route bookkeeping and bounce back to `validating` so the
    # reviewer re-evaluates against the current head instead of leaving the
    # issue stuck in `fixing` with no work.
    if not feedback.all_items:
        _owner._clear_pending_fix_bookmarks(state)
        gh.set_workflow_label(issue, WorkflowLabel.VALIDATING)
        gh.write_pinned_state(issue, state)
        return

    if _owner._fixing_debounce_open(feedback, replay_batch):
        return

    _owner._resume_fixing_and_dispatch_result(
        _FixingContext(gh, spec, issue, state, pr), feedback, replay_batch,
    )
