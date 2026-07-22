# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""In review drift."""
from __future__ import annotations

from orchestrator.stages import in_review as _owner

_DriftResume = _owner._DriftResume
_InReviewContext = _owner._InReviewContext
Issue = _owner.Issue
WorkflowLabel = _owner.WorkflowLabel
filter_trusted = _owner.filter_trusted


def _route_feedback_to_fixing(
    ctx: _InReviewContext,
    issue_space_new: list,
    review_space_new: list,
    review_summary_new: list,
) -> None:
    """Hand fresh PR feedback off to the `fixing` stage instead of silently
    waiting through the debounce window or spawning the dev agent here.
    Recording the per-namespace ids in pinned state (see
    `_record_pending_fix_bookmarks`) gives the fixing handler a bookmark of what
    triggered the route so it can resume the dev session, push a fix, and flip
    back to `validating` -- all without `_handle_in_review` keeping the
    comment-debounce / dev-resume machinery in its own body.

    Deliberately NOT honoring the debounce window before the flip: with the
    route to `fixing`, the dev is no longer spawned from this handler at all --
    the fixing stage owns debouncing before its own spawn, so flipping
    immediately is the right contract (the `fixing` label surfaces the
    transition to the operator straight away, and any concurrent additional
    comments are seen by the fixing handler on its next tick).

    Refresh `user_content_hash` so the user-content drift detection does NOT
    fire on the next tick for the same comment changes just consumed via the
    fixing route: the hash covers title + body + human issue-thread comments, so
    any issue-thread comment in `issue_space_new` shifts it; leaving the old
    hash would have the drift path resume the dev and bounce to `validating` the
    moment a human relabels the issue back to `in_review`, undoing the route.
    """
    from orchestrator import workflow as _wf

    state = ctx.state
    state.set("pending_fix_at", _wf._now_iso())
    _owner._record_pending_fix_bookmarks(
        state, issue_space_new, review_space_new, review_summary_new,
    )
    state.set(
        "user_content_hash",
        _wf._compute_user_content_hash(ctx.issue, _wf._orchestrator_ids(state)),
    )
    # If we were parked awaiting human, the comment that triggered this route is
    # the human signal -- clear the park flags so the fixing handler is not
    # greeted with stale awaiting_human state.
    state.set("awaiting_human", False)
    state.set("park_reason", None)
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.FIXING)
    ctx.gh.write_pinned_state(ctx.issue, state)


def _build_drift_resume_prompt(issue: Issue, unread_pr_conv: list) -> str:
    """Assemble the dev-resume prompt for a user-content drift: the recent
    issue-thread conversation combined with any unread PR-conversation
    comments so the dev sees both surfaces before the watermark bump consumes
    them.
    """
    from orchestrator import workflow as _wf

    comments_text = _wf._recent_comments_text(issue)
    if unread_pr_conv:
        pr_block = "\n\n".join(
            _wf._quote_comment_line(comment, label=" (PR comment)")
            for comment in unread_pr_conv
        )
        prefix = f"{comments_text}\n\n" if comments_text else ""
        comments_text = (
            f"{prefix}Unread PR conversation comments:\n\n{pr_block}"
        )
    return _wf._build_user_content_change_prompt(issue, comments_text)


def _drift_unread_pr_conv(ctx: _InReviewContext) -> list:
    """Capture unread PR-conversation comments BEFORE the drift notice and the
    later watermark bump.

    The issue thread and PR conversation share the IssueComment id space, so
    `_bump_in_review_watermarks` (driven by issue-thread ids only) can leap past
    a PR-conversation comment whose id falls between the prior
    `pr_last_comment_id` and the new issue-thread max -- the dev would never see
    it. Capturing those comments here and quoting them in the followup prompt is
    what stops a concurrent PR comment from being silently dropped. Orchestrator
    id / marker filtering mirrors the regular in_review comment scan.
    """
    from orchestrator import workflow as _wf

    issue_wm = _owner._issue_side_watermark(ctx.state)
    orchestrator_ids = _wf._orchestrator_ids(ctx.state)
    return _owner._drop_orchestrator_comments(
        ctx.gh.pr_conversation_comments_after(ctx.pr, issue_wm), orchestrator_ids,
    )


def _drift_worktree(ctx: _InReviewContext):
    """Resolve the PR worktree for the drift resume, recreating it on the
    resolved branch if the path is gone.
    """
    from orchestrator import workflow as _wf

    wt = _wf._worktree_path(ctx.spec, ctx.issue.number)
    if not wt.exists():
        wt = _wf._ensure_worktree(
            ctx.spec, ctx.issue.number,
            branch=_wf._resolve_branch_name(ctx.state, ctx.spec, ctx.issue.number),
        )
    return wt


def _resume_dev_for_drift(
    ctx: _InReviewContext, unread_pr_conv: list,
) -> _DriftResume:
    """Notify both surfaces, mark the issue-thread drift comments consumed,
    resolve the worktree, and resume the locked dev session with the updated
    body plus the unread PR conversation. Captures the pre-resume HEAD so the
    disposition can tell a pushed fix from a no-commit ack.

    The dev sees the full issue thread via `_recent_comments_text` in the resume
    prompt, so marking the issue-thread comments consumed here keeps both a
    later validating->in_review handoff and the in_review watermark check from
    replaying them as fresh feedback. Untrusted authors are filtered out of the
    quoted PR-conversation block; the watermark bump still consumes the raw
    `unread_pr_conv` so an outsider comment is not re-scanned next tick.
    """
    from orchestrator import workflow as _wf

    _wf._post_pr_comment(
        ctx.gh, int(ctx.pr_number), ctx.state,
        ":pencil2: issue body changed; resuming dev session.",
    )
    _wf._mark_drift_comments_consumed(ctx.gh, ctx.issue, ctx.state)
    wt = _owner._drift_worktree(ctx)
    before_sha = _wf._head_sha(wt)
    wt, dev_result, paused = _wf._resume_dev_with_text(
        ctx.gh, ctx.spec, ctx.issue, ctx.state,
        _owner._build_drift_resume_prompt(ctx.issue, filter_trusted(unread_pr_conv)),
        pause_guard=True,
    )
    ctx.state.set("last_agent_action_at", _wf._now_iso())
    return _DriftResume(
        worktree=wt, dev_result=dev_result, paused=paused, before_sha=before_sha,
    )


def _dispose_drift_result(
    ctx: _InReviewContext, unread_pr_conv: list, resume: _DriftResume,
) -> None:
    """Post the dev result (a no-commit reply is an ack, not a park), ratchet
    the in_review issue-side watermark past everything consumed this tick, and
    on either outcome (pushed fix or ack) bounce DIRECTLY back to `validating`
    with `review_round` reset.

    The drift invalidated the prior validation either way: the reviewer approved
    against the OLD requirements, so `review_round` must reset before the issue
    can earn a fresh approval. Docs do not run here; the single docs pass is
    deferred to the final-docs handoff after reviewer approval. Passing
    `unread_pr_conv` to the bump includes PR-conversation ids ABOVE the
    issue-thread max in the candidate set; without it a PR comment with id
    higher than every issue-thread id would survive the bump and re-fire as
    fresh feedback.
    """
    from orchestrator import workflow as _wf

    outcome = _wf._post_user_content_change_result(
        ctx.gh, ctx.spec, ctx.issue, ctx.state,
        resume.worktree, resume.dev_result, resume.before_sha,
    )
    _owner._bump_in_review_watermarks(ctx, issue_space_new=unread_pr_conv)
    if outcome in ("pushed", "ack"):
        ctx.state.set("review_round", 0)
        ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _handle_user_content_drift(ctx: _InReviewContext) -> bool:
    """Resume the dev when a human edited the issue title / body after the PR
    opened (no fresh comment surface triggered the fixing route).

    Returns True when drift was detected and handled (the caller must return),
    False when there is no drift (the caller falls through to the mergeability
    gate).
    """
    from orchestrator import workflow as _wf

    new_hash = _wf._detect_user_content_change(ctx.gh, ctx.issue, ctx.state)
    if new_hash is None:
        return False
    ctx.state.set("user_content_hash", new_hash)
    unread_pr_conv = _owner._drift_unread_pr_conv(ctx)
    resume = _owner._resume_dev_for_drift(ctx, unread_pr_conv)
    # Interrupted (shutdown sweep) or live-paused (operator added `paused` /
    # `backlog` mid-run) resume: bail WITHOUT writing pinned state so everything
    # staged above -- refreshed `user_content_hash`, consumed drift comments,
    # `last_agent_action_at`, the `awaiting_human` clear inside
    # `_resume_dev_with_text` -- is discarded and the next process re-detects the
    # body change and leaves any committed work on the branch. Must precede
    # `_dispose_drift_result` so it neither parses a partial reply nor persists
    # the consumption.
    if _wf._ignore_if_interrupted(ctx.issue, resume.dev_result):
        return True
    if resume.paused:
        return True
    _owner._dispose_drift_result(ctx, unread_pr_conv, resume)
    return True
