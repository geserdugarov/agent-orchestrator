# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""A body edit that lands after the PR is already open.

The dev session is still the one that wrote the branch, so the edit resumes it
rather than re-deciding the work. What makes this route different from the
same edit during implementing is where it ends: both a pushed fix and a
no-commit `ACK:` hand the issue back to `validating` with `review_round`
reset, because the approval that carried it to `in_review` was earned against
requirements that no longer exist. Docs deliberately do not run on the way
out -- the single docs pass belongs to the final-docs handoff after a fresh
reviewer approval.

The PR conversation is read BEFORE the notice and the ratchet, and that
ordering is the whole reason `_drift_unread_pr_conv` exists: the issue thread
and the PR conversation share one id space, so the issue-side ratchet can leap
past a PR comment whose id happens to fall inside the range it advances
through. Capturing those comments up front and quoting them into the resume
prompt is what keeps a concurrent PR comment from vanishing unanswered.

Both refusals sit between the finished run and the disposition rather than
before the run, because the run itself is what makes them decidable: a
shutdown-interrupted result and a live pause both bail WITHOUT writing pinned
state, so the refreshed hash, the consumed comments, and the cleared park are
all discarded and the next process re-detects the same edit.
"""
from __future__ import annotations

from github.Issue import Issue

from orchestrator.git.verification import probes as _verification_probes
from orchestrator.git.worktrees import creation as _worktree_creation
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.github.comments import filter_trusted
from orchestrator.workflow.engine import comments as _comments
from orchestrator.workflow.engine import drift as _engine_drift
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.stages.implementing import resume as _dev_resume
from orchestrator.workflow.stages.in_review import feedback as _feedback
from orchestrator.workflow.stages.in_review import models as _models
from orchestrator.workflow.stages.in_review import watermarks as _watermarks
from orchestrator.workflow.stages.validating import drift_outcomes as _drift_outcomes
from orchestrator.workflow.state import WorkflowLabel


def _build_drift_resume_prompt(issue: Issue, unread_pr_conv: list) -> str:
    """Assemble the dev-resume prompt for a user-content drift: the recent
    issue-thread conversation combined with any unread PR-conversation
    comments so the dev sees both surfaces before the watermark bump consumes
    them.
    """
    comments_text = _comments._recent_comments_text(issue)
    if unread_pr_conv:
        pr_block = "\n\n".join(
            _comments._quote_comment_line(comment, label=" (PR comment)")
            for comment in unread_pr_conv
        )
        prefix = f"{comments_text}\n\n" if comments_text else ""
        comments_text = (
            f"{prefix}Unread PR conversation comments:\n\n{pr_block}"
        )
    return _engine_drift._build_user_content_change_prompt(issue, comments_text)


def _drift_unread_pr_conv(ctx: _models._InReviewContext) -> list:
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
    issue_wm = _feedback._issue_side_watermark(ctx.state)
    orchestrator_ids = _comments._orchestrator_ids(ctx.state)
    return _feedback._drop_orchestrator_comments(
        ctx.gh.pr_conversation_comments_after(ctx.pr, issue_wm), orchestrator_ids,
    )


def _drift_worktree(ctx: _models._InReviewContext):
    """Resolve the PR worktree for the drift resume, recreating it on the
    resolved branch if the path is gone.
    """
    wt = _worktree_paths._worktree_path(ctx.spec, ctx.issue.number)
    if not wt.exists():
        wt = _worktree_creation._ensure_worktree(
            ctx.spec, ctx.issue.number,
            branch=_worktree_paths._resolve_branch_name(
                ctx.state, ctx.spec, ctx.issue.number,
            ),
        )
    return wt


def _resume_dev_for_drift(
    ctx: _models._InReviewContext, unread_pr_conv: list,
) -> _models._DriftResume:
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
    _comments._post_pr_comment(
        ctx.gh, int(ctx.pr_number), ctx.state,
        ":pencil2: issue body changed; resuming dev session.",
    )
    _engine_drift._mark_drift_comments_consumed(ctx.gh, ctx.issue, ctx.state)
    wt = _drift_worktree(ctx)
    before_sha = _verification_probes._head_sha(wt)
    wt, dev_result, paused = _dev_resume._resume_dev_with_text(
        ctx.gh, ctx.spec, ctx.issue, ctx.state,
        _build_drift_resume_prompt(ctx.issue, filter_trusted(unread_pr_conv)),
        pause_guard=True,
    )
    ctx.state.set("last_agent_action_at", _usage._now_iso())
    return _models._DriftResume(
        worktree=wt, dev_result=dev_result, paused=paused, before_sha=before_sha,
    )


def _dispose_drift_result(
    ctx: _models._InReviewContext,
    unread_pr_conv: list,
    resume: _models._DriftResume,
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
    outcome = _drift_outcomes._post_user_content_change_result(
        ctx.gh, ctx.spec, ctx.issue, ctx.state,
        resume.worktree, resume.dev_result, resume.before_sha,
    )
    _watermarks._bump_in_review_watermarks(ctx, issue_space_new=unread_pr_conv)
    if outcome in ("pushed", "ack"):
        ctx.state.set("review_round", 0)
        ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _handle_user_content_drift(ctx: _models._InReviewContext) -> bool:
    """Resume the dev when a human edited the issue title / body after the PR
    opened (no fresh comment surface triggered the fixing route).

    Returns True when drift was detected and handled (the caller must return),
    False when there is no drift (the caller falls through to the mergeability
    gate).
    """
    new_hash = _engine_drift._detect_user_content_change(ctx.gh, ctx.issue, ctx.state)
    if new_hash is None:
        return False
    ctx.state.set("user_content_hash", new_hash)
    unread_pr_conv = _drift_unread_pr_conv(ctx)
    resume = _resume_dev_for_drift(ctx, unread_pr_conv)
    # Interrupted (shutdown sweep) or live-paused (operator added `paused` /
    # `backlog` mid-run) resume: bail WITHOUT writing pinned state so everything
    # staged above -- refreshed `user_content_hash`, consumed drift comments,
    # `last_agent_action_at`, the `awaiting_human` clear inside
    # `_resume_dev_with_text` -- is discarded and the next process re-detects the
    # body change and leaves any committed work on the branch. Must precede
    # `_dispose_drift_result` so it neither parses a partial reply nor persists
    # the consumption.
    if _guards._ignore_if_interrupted(ctx.issue, resume.dev_result):
        return True
    if resume.paused:
        return True
    _dispose_drift_result(ctx, unread_pr_conv, resume)
    return True
