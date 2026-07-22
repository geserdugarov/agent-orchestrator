# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fixing feedback."""
from __future__ import annotations

from orchestrator.stages import fixing as _owner

_FixingFeedback = _owner._FixingFeedback
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
filter_trusted = _owner.filter_trusted


def _new_issue_space_feedback(gh: GitHubClient, issue: Issue, pr, state) -> list:
    """Unread issue-thread + PR-conversation comments past the in_review
    watermark, sorted by id, with orchestrator comments and untrusted authors
    dropped.

    The two surfaces share the IssueComment id namespace, so one watermark
    covers both. Mirror `_handle_in_review`'s fallback: if no PR-side
    watermark exists yet (an in_review tick that routed to `fixing` before
    ever seeding `pr_last_comment_id` -- e.g. a manual relabel into
    `in_review` without going through validating, or a legacy issue that
    pre-dates the watermark migration), fall back to `last_action_comment_id`.
    Without this, `comments_after` / `pr_conversation_comments_after` would be
    called with `after_id=None` and re-feed every historical comment into the
    dev's `_build_pr_comment_followup` prompt as fresh feedback.

    Orchestrator comments are filtered by id AND the hidden body marker -- the
    id cap evicts old ids on long-lived issues, after which an id-only filter
    would start re-feeding old bot comments to the dev. Untrusted authors are
    dropped last (see `filter_trusted`) so an outsider's comment never resumes
    the dev or extends the debounce window; an empty allowlist trusts everyone.
    """
    from orchestrator import workflow as _wf

    issue_wm = state.get("pr_last_comment_id")
    if issue_wm is None:
        issue_wm = state.get("last_action_comment_id")
    orchestrator_ids = _wf._orchestrator_ids(state)
    unread = [
        comment
        for comment in list(gh.comments_after(issue, issue_wm))
        + list(gh.pr_conversation_comments_after(pr, issue_wm))
        if comment.id not in orchestrator_ids
        and _wf._ORCH_COMMENT_MARKER not in (comment.body or "")
    ]
    return filter_trusted(sorted(unread, key=lambda comment: comment.id))


def _new_review_comment_feedback(gh: GitHubClient, pr, state) -> list:
    """Unread inline review comments past `pr_last_review_comment_id`, sorted
    by id and trust-filtered.

    Inline review comments live in their own id space the orchestrator never
    posts on, so no orchestrator filter is needed -- only the trust gate.
    """
    review_wm = state.get("pr_last_review_comment_id")
    return filter_trusted(sorted(
        gh.pr_inline_comments_after(pr, review_wm),
        key=lambda comment: comment.id,
    ))


def _new_review_summary_feedback(gh: GitHubClient, pr, state) -> list:
    """Unread review summaries past `pr_last_review_summary_id`, sorted by id
    and trust-filtered (same rationale as `_new_review_comment_feedback`).
    """
    review_summary_wm = state.get("pr_last_review_summary_id")
    return filter_trusted(sorted(
        gh.pr_reviews_after(pr, review_summary_wm),
        key=lambda review: review.id,
    ))


def _rescan_fixing_feedback(
    gh: GitHubClient, issue: Issue, pr, state,
) -> _FixingFeedback:
    """Rescan the four PR-feedback surfaces for comments past the in_review
    watermarks (NOT the `pending_fix_*` bookmarks -- those stay in pinned
    state as the reconstruction source for `_reconstruct_pending_fix_batch`).

    Returns the three per-surface batches plus `all_items`, concatenated in
    prompt order: issue-space (issue-thread + PR-conversation), then inline
    review comments, then review summaries.
    """
    issue_space = _owner._new_issue_space_feedback(gh, issue, pr, state)
    review_comments = _owner._new_review_comment_feedback(gh, pr, state)
    review_summaries = _owner._new_review_summary_feedback(gh, pr, state)
    return _FixingFeedback(
        issue_space=issue_space,
        review_comments=review_comments,
        review_summaries=review_summaries,
        all_items=issue_space + review_comments + review_summaries,
    )
