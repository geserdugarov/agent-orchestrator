# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fixing continue."""
from __future__ import annotations

from orchestrator.stages import _fixing_state as _state
from orchestrator.stages import fixing as _owner

_FixingContext = _owner._FixingContext
_FixingFeedback = _owner._FixingFeedback
config = _owner.config
filter_trusted = _owner.filter_trusted
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_PARK_REASON = _state._PARK_REASON
_PENDING_FIX_AT = _state._PENDING_FIX_AT


def _reconstruct_pending_fix_batch(gh, issue, pr, state) -> list:
    """Rebuild the exact feedback batch that drove the `in_review` -> `fixing`
    route from the pinned `pending_fix_*` metadata.

    The per-tick rescan in `_handle_fixing` reads from the in_review
    watermarks, which advance past the triggering feedback the moment a dev
    resume consumes it -- so once a fix has been attempted the batch can no
    longer be recovered by rescanning. This helper reconstructs it from the
    persisted ids instead, returned in the same order the route built them --
    issue-space (issue-thread + PR-conversation) then inline review comments
    then review summaries, each sorted by id. Filtering by the recorded id set
    inherently drops the orchestrator's own comments (their ids were never in
    the batch) and survives watermark advancement because the fetch is
    unbounded. A batch item deleted on GitHub since the route simply drops out.

    The validating -> fixing route preserves no `pending_fix_*_ids`; its lone
    replay anchor is the reviewer-feedback PR comment recorded in
    `pending_fix_reviewer_comment_id`. `_reviewer_anchor_comment` re-fetches it
    and it is prepended to the batch OUTSIDE `filter_trusted` (it is the
    orchestrator's own trusted reviewer output, which the author allowlist
    would otherwise drop). Consulted ONLY on the validating route
    (`pending_fix_at` unset): a stale anchor left behind by an earlier
    validating park must not be prepended to an in_review-route batch. The two
    routes are mutually exclusive in practice, so the anchor is de-duplicated
    against the id-set batch defensively.

    Re-apply the author allowlist at reconstruction time, not only at route
    time: an issue parked before the trust gate shipped can carry untrusted ids
    in `pending_fix_*_ids`, and `ALLOWED_ISSUE_AUTHORS` may change between the
    route and the `/orchestrator continue` replay. Existing parked issues that
    carry only `pending_fix_*_max_id` (no id lists) get the conservative
    single-item reconstruction from `_pending_fix_id_set`.
    """
    trusted_batch = filter_trusted(
        _owner._reconstruct_issue_space(gh, issue, pr, state)
        + _owner._reconstruct_review_comments(gh, pr, state)
        + _owner._reconstruct_review_summaries(gh, pr, state)
    )
    if state.get(_PENDING_FIX_AT) is None:
        anchor = _owner._reviewer_anchor_comment(gh, pr, state)
        if anchor is not None and all(
            feedback_item.id != anchor.id for feedback_item in trusted_batch
        ):
            return [anchor] + trusted_batch
    return trusted_batch


def _advance_consumed_watermarks(
    state, feedback: _FixingFeedback,
) -> None:
    """Advance the three in_review watermarks ONLY to the max id consumed
    per surface, ratcheted against the existing watermark.

    Called once on every dev-result outcome (BOTH the pushed-fix path
    AND the park/failure path) before the pushed/non-pushed split, so
    a concurrent human comment that landed between `feedback` and
    this call survives to the next tick on either branch. The broader
    `_bump_in_review_watermarks` is deliberately NOT used here: it
    also pulls in `gh.latest_comment_id(issue)`, which could leap the
    watermark past a concurrent issue-thread comment the dev never saw
    in its prompt -- silently swallowing real feedback on the pushed
    path (the next in_review tick would miss it) and on the
    park/failure path (the next fixing tick's
    `awaiting_human and not new_feedback` gate would drop it).
    """
    cur_issue_wm = state.get("pr_last_comment_id")
    if feedback.issue_space:
        new_wm = max(comment.id for comment in feedback.issue_space)
        if isinstance(cur_issue_wm, int):
            new_wm = max(new_wm, cur_issue_wm)
        state.set("pr_last_comment_id", new_wm)

    cur_review_wm = state.get("pr_last_review_comment_id")
    if feedback.review_comments:
        new_wm = max(comment.id for comment in feedback.review_comments)
        if isinstance(cur_review_wm, int):
            new_wm = max(new_wm, cur_review_wm)
        state.set("pr_last_review_comment_id", new_wm)

    cur_summary_wm = state.get("pr_last_review_summary_id")
    if feedback.review_summaries:
        new_wm = max(review.id for review in feedback.review_summaries)
        if isinstance(cur_summary_wm, int):
            new_wm = max(new_wm, cur_summary_wm)
        state.set("pr_last_review_summary_id", new_wm)


def _handle_continue_command(
    ctx: _FixingContext,
    feedback: _FixingFeedback,
) -> tuple:
    """Dispatch a `/orchestrator continue` operator command on a parked
    `fixing` issue.

    `/orchestrator continue` is the operator's "retry this fix" signal for a
    session-limit / session-failure park: a dev session that went silent
    (`agent_silent`) or timed out (`agent_timeout`) and left the fix-loop
    parked. The naive un-park resumes the dev on the command text alone,
    dropping the PR review feedback the poisoned session never addressed --
    the geserdugarov/lance-open-source#23 shape.

    Returns `(action, items)`:

      * ``("replay", batch)`` -- an eligible park WITH a reconstructable batch
        (the in_review `pending_fix_*` bookmarks, or the validating-route
        `pending_fix_reviewer_comment_id` anchor). Drops the poisoned dev
        session (so the retry re-grounds a FRESH session on the committed
        branch rather than replaying the transcript that already failed) and
        clears the park, as side effects; `batch` is the preserved PR-feedback
        batch (`_reconstruct_pending_fix_batch`) followed by ALL fresh feedback
        verbatim -- the command comment AND any guidance posted with or beside
        it -- so nothing the operator wrote is dropped. Pinned state is NOT
        written here (the caller's resume tail writes it).
      * ``("refuse", None)`` -- a content-free continue (every fresh comment is
        a bare command) on a park it cannot retry: an unsafe park that still
        needs real human guidance, or an eligible park with no reconstructable
        batch (a validating-route park whose reviewer anchor was never recorded
        or has since been deleted). Consumes the command comment (so the
        refusal does not re-fire) and posts the reason; the caller writes state
        and the issue stays parked.
      * ``("passthrough", None)`` -- the command arrived alongside genuine
        guidance on a park with no replayable batch. No side effect; the caller
        runs the normal resume so that guidance (not a bare continue) drives
        the dev.
    """
    from orchestrator import workflow as _wf

    park_reason = ctx.state.get(_PARK_REASON)
    batch = (
        _owner._reconstruct_pending_fix_batch(ctx.gh, ctx.issue, ctx.pr, ctx.state)
        if park_reason in _wf._CONTINUE_PARK_REASONS else []
    )
    if batch:
        _wf._drop_poisoned_dev_session(ctx.state)
        ctx.state.set(_AWAITING_HUMAN, False)
        ctx.state.set(_PARK_REASON, None)
        _wf.log.info(
            "issue=#%s /orchestrator continue: replaying %d preserved feedback "
            "item(s) on a fresh dev session (park_reason=%s)",
            ctx.issue.number, len(batch), park_reason,
        )
        # Carry every fresh comment (command + any accompanying guidance)
        # verbatim into the replay: the resume tail advances the watermarks
        # past all of `feedback`, so anything omitted here would be consumed
        # without the dev ever seeing it.
        return "replay", batch + feedback.all_items

    if all(
        _wf._is_bare_orchestrator_continue(comment)
        for comment in feedback.all_items
    ):
        # Content-free continue with nothing else to act on. Consume only the
        # command comment(s) (`feedback.all_items` is all bare commands here,
        # so `continue_cmds` covers them) so the refusal is not re-posted every
        # tick, then stay parked with a reason.
        continue_cmds = _wf._parse_orchestrator_continue(feedback.issue_space)
        command_feedback = _FixingFeedback(
            issue_space=continue_cmds,
            review_comments=[],
            review_summaries=[],
            all_items=continue_cmds,
        )
        _owner._advance_consumed_watermarks(ctx.state, command_feedback)
        if park_reason in _wf._CONTINUE_PARK_REASONS:
            message = (
                f"{config.HITL_MENTIONS} `/orchestrator continue`: no "
                "preserved PR-feedback batch is on file to replay for this "
                "park. Reply with the change to make, or relabel the issue, "
                "to proceed."
            )
        else:
            message = (
                f"{config.HITL_MENTIONS} `/orchestrator continue` needs your "
                "actual guidance here: this park is waiting on a real answer "
                "(an agent question, or a worktree it could not finish), not "
                "a generic continue. Reply with the specific change to make, "
                "or relabel the issue, to proceed."
            )
        _wf._post_issue_comment(ctx.gh, ctx.issue, ctx.state, message)
        return "refuse", None

    # The command came WITH genuine guidance on a park with no replayable
    # batch; let the normal resume feed that guidance to the dev.
    return "passthrough", None
