# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fixing bookmarks."""
from __future__ import annotations

from orchestrator.stages import _fixing_state as _state
from orchestrator.stages import fixing as _owner

_PENDING_FIX_AT = _state._PENDING_FIX_AT


def _clear_pending_fix_bookmarks(state) -> None:
    state.set(_PENDING_FIX_AT, None)
    state.set("pending_fix_issue_max_id", None)
    state.set("pending_fix_review_max_id", None)
    state.set("pending_fix_review_summary_max_id", None)
    state.set("pending_fix_issue_ids", None)
    state.set("pending_fix_review_ids", None)
    state.set("pending_fix_review_summary_ids", None)
    # Validating-route reviewer-feedback replay anchor (recorded by
    # `_handle_validating_changes_requested`). Cleared alongside the
    # in_review-route bookmarks so a later route writes fresh values and a
    # session-failure park never replays an already-addressed reviewer round.
    state.set("pending_fix_reviewer_comment_id", None)


def _pending_fix_id_set(state, ids_key: str, max_id_key: str) -> set:
    """Resolve the persisted batch ids for one feedback surface.

    Prefers the full `pending_fix_*_ids` list the in_review route records.
    Falls back -- conservatively -- to the single `pending_fix_*_max_id`
    for issues parked before the id lists existed: the max id is the only
    member a legacy bookmark can vouch for, so the reconstruction includes
    just that one item rather than guessing a lower bound the advanced
    watermark can no longer supply. `bool` is rejected explicitly because
    it is an `int` subclass and a stray `True` must not read as id 1.
    """
    ids = state.get(ids_key)
    if isinstance(ids, list) and ids:
        return {int(comment_id) for comment_id in ids}
    max_id = state.get(max_id_key)
    if isinstance(max_id, int) and not isinstance(max_id, bool):
        return {max_id}
    return set()


def _reviewer_anchor_comment(gh, pr, state):
    """Fetch the validating-route reviewer-feedback replay anchor, or None.

    `_handle_validating_changes_requested` posts the automated reviewer's
    CHANGES_REQUESTED feedback as one PR-conversation comment and records its
    id in `pending_fix_reviewer_comment_id` (WITHOUT setting `pending_fix_at`,
    which discriminates the two routes' review-round accounting). That route
    preserves no `pending_fix_*_ids`, so this single comment is the only
    replayable input for a `/orchestrator continue` on a session-failure park
    that came through validating.

    Re-fetch it by id from the PR conversation surface. The comment is
    orchestrator-authored -- normally dropped from a rescan by the id-set
    filter and by `filter_trusted` when the PAT login is not allowlisted --
    but it carries the reviewer's own trusted feedback, so the caller adds it
    OUTSIDE the trust filter. `bool` is rejected explicitly (it is an `int`
    subclass and a stray `True` must not read as id 1). Returns None when the
    anchor id is unset / not an int, or the comment can no longer be fetched
    (deleted, or a PR read that returned without it) -- the empty-batch
    refusal then holds.
    """
    anchor_id = state.get("pending_fix_reviewer_comment_id")
    if not isinstance(anchor_id, int) or isinstance(anchor_id, bool):
        return None
    for pr_comment in gh.pr_conversation_comments_after(pr, None):
        if pr_comment.id == anchor_id:
            return pr_comment
    return None


def _reconstruct_issue_space(gh, issue, pr, state) -> list:
    """Batch items from the shared issue-thread + PR-conversation id space.

    Re-fetches both surfaces in full (`after_id=None`) and keeps only the ids
    recorded at route time, sorted by id -- so the reconstruction survives the
    watermark advancement that follows the first dev resume.
    """
    issue_ids = _owner._pending_fix_id_set(
        state, "pending_fix_issue_ids", "pending_fix_issue_max_id",
    )
    if not issue_ids:
        return []
    matched = [
        issue_comment
        for issue_comment in gh.comments_after(issue, None)
        if issue_comment.id in issue_ids
    ]
    matched += [
        pr_comment
        for pr_comment in gh.pr_conversation_comments_after(pr, None)
        if pr_comment.id in issue_ids
    ]
    matched.sort(key=lambda comment: comment.id)
    return matched


def _reconstruct_review_comments(gh, pr, state) -> list:
    """Inline review-comment batch items recorded at route time, sorted by id."""
    review_ids = _owner._pending_fix_id_set(
        state, "pending_fix_review_ids", "pending_fix_review_max_id",
    )
    if not review_ids:
        return []
    matched = [
        review_comment
        for review_comment in gh.pr_inline_comments_after(pr, None)
        if review_comment.id in review_ids
    ]
    matched.sort(key=lambda comment: comment.id)
    return matched


def _reconstruct_review_summaries(gh, pr, state) -> list:
    """Review-summary batch items recorded at route time, sorted by id."""
    summary_ids = _owner._pending_fix_id_set(
        state,
        "pending_fix_review_summary_ids",
        "pending_fix_review_summary_max_id",
    )
    if not summary_ids:
        return []
    matched = [
        review
        for review in gh.pr_reviews_after(pr, None)
        if review.id in summary_ids
    ]
    matched.sort(key=lambda review: review.id)
    return matched
