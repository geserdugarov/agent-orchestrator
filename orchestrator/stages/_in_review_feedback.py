# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""In review feedback."""
from __future__ import annotations

from orchestrator.stages import _in_review_state as _state
from orchestrator.stages import in_review as _owner

_InReviewContext = _owner._InReviewContext
Optional = _owner.Optional
PinnedState = _owner.PinnedState
filter_trusted = _owner.filter_trusted
_PR_LAST_COMMENT_ID = _state._PR_LAST_COMMENT_ID


def _final_docs_handoff_completed_for_head(
    state: PinnedState, head_sha: str,
) -> bool:
    """True when the reviewer-approved final-docs handoff covers `head_sha`."""
    if not head_sha:
        return False
    return (
        state.get("docs_checked_sha") == head_sha
        and state.get("docs_verdict") in ("updated", "no_change")
    )


def _drop_orchestrator_comments(comments, orchestrator_ids) -> list:
    """Keep only genuine human feedback from an issue-thread / PR-conversation
    comment stream.

    Issue-thread and PR-conversation comments share the IssueComment id
    namespace. Filter orchestrator comments by recorded id AND by the hidden
    body marker: older state can miss an id, and the bounded id list can
    eventually evict it, but the marker stays on the GitHub comment.
    """
    from orchestrator import workflow as _wf

    return [
        comment
        for comment in comments
        if comment.id not in orchestrator_ids
        and _wf._ORCH_COMMENT_MARKER not in (comment.body or "")
    ]


def _issue_side_watermark(state: PinnedState) -> Optional[int]:
    """Resolve the issue / PR-conversation scan watermark.

    `or` would discard a legacy default of `pr_last_comment_id == 0` and fall
    back to `last_action_comment_id` (the id of a prior park comment), which
    sits ABOVE any human "do not merge yet" comment posted earlier during
    implementing / validating; that human comment would then never surface as
    fresh PR feedback. Treat 0 as a valid "scan from the beginning" watermark.
    """
    issue_wm = state.get(_PR_LAST_COMMENT_ID)
    if issue_wm is None:
        issue_wm = state.get("last_action_comment_id")
    return issue_wm


def _fresh_issue_space(ctx: _InReviewContext, orchestrator_ids) -> list:
    """Merge fresh issue-thread and PR-conversation feedback -- one shared
    IssueComment id namespace -- into a single stream: drop orchestrator
    comments, drop untrusted authors, sort ascending by id. Filtering untrusted
    authors here keeps an outsider's issue / PR comment from bookmarking a
    pending fix or steering the `in_review` -> `fixing` route.
    """
    issue_wm = _owner._issue_side_watermark(ctx.state)
    new_issue_side = _owner._drop_orchestrator_comments(
        ctx.gh.comments_after(ctx.issue, issue_wm), orchestrator_ids,
    )
    new_pr_conv = _owner._drop_orchestrator_comments(
        ctx.gh.pr_conversation_comments_after(ctx.pr, issue_wm), orchestrator_ids,
    )
    return filter_trusted(sorted(
        list(new_issue_side) + list(new_pr_conv),
        key=lambda comment: comment.id,
    ))


def _scan_fresh_pr_feedback(ctx: _InReviewContext):
    """Collect fresh, human-authored feedback across the four in_review
    surfaces (issue thread, PR conversation, inline review, review summary).

    Returns `(issue_space_new, review_space_new, review_summary_new)`, each
    already sorted ascending by id. The issue-thread and PR-conversation
    streams share one id namespace and are merged into `issue_space_new`.
    Untrusted authors are dropped from every surface (see `filter_trusted`) so
    outsider feedback cannot bookmark a pending fix or route to `fixing`; the
    orchestrator marker/id filtering is layered underneath it. An empty
    allowlist trusts everyone, so the default deployment is unchanged.
    """
    from orchestrator import workflow as _wf

    orchestrator_ids = _wf._orchestrator_ids(ctx.state)
    issue_space_new = _owner._fresh_issue_space(ctx, orchestrator_ids)
    review_space_new = filter_trusted(sorted(
        ctx.gh.pr_inline_comments_after(
            ctx.pr, ctx.state.get("pr_last_review_comment_id"),
        ),
        key=lambda comment: comment.id,
    ))
    review_summary_new = filter_trusted(sorted(
        ctx.gh.pr_reviews_after(
            ctx.pr, ctx.state.get("pr_last_review_summary_id"),
        ),
        key=lambda review: review.id,
    ))
    return issue_space_new, review_space_new, review_summary_new


def _stay_parked(state: PinnedState, new_comments: list) -> bool:
    """True when an awaiting-human park must stay silent this tick.

    Two cases collapse here:

    * A prior tick already parked on an unrecoverable state and nothing
      changed since -- the human action that unsticks us is a comment, a
      relabel, or closing / merging the PR. The first two land in
      `new_comments`; the last two are caught by the terminal drain above.
    * The park belongs to the `_sync_pr_worktree_to_base` retry loop
      (`_AUTO_REBASE_PARK_REASONS`). A fresh human comment there is the
      operator's "retry the rebase" signal that the base-sync refresh owns,
      NOT fresh PR feedback to route to `fixing`. Staying silent keeps the
      refresh in control of the comment; routing here would consume it as
      feedback and silently drop the retry intent.
    """
    from orchestrator import workflow as _wf

    if not state.get("awaiting_human"):
        return False
    return (
        not new_comments
        or state.get("park_reason") in _wf._AUTO_REBASE_PARK_REASONS
    )


def _record_pending_fix_bookmarks(
    state: PinnedState,
    issue_space_new: list,
    review_space_new: list,
    review_summary_new: list,
) -> None:
    """Bookmark the fresh-feedback batch for the fixing handler: per surface,
    the max id (the existing pinned-state contract and the conservative
    reconstruction bound for issues parked before the id lists existed) plus the
    full id list, so a later fixing tick reconstructs the EXACT triggering batch
    even after the in_review watermarks advance past it -- the max id alone
    loses the batch's lower members once a rescan can no longer reach them.
    `_reconstruct_pending_fix_batch` prefers the id lists. Each list is already
    sorted ascending by id (sorted at scan time).

    These are bookmarks, not watermarks: they are deliberately NOT bumped past
    the batch, because the fixing handler re-reads these same comments to build
    its dev-resume prompt and consuming them now would lose the triggering
    feedback.
    """
    for max_key, ids_key, batch in (
        ("pending_fix_issue_max_id", "pending_fix_issue_ids", issue_space_new),
        ("pending_fix_review_max_id", "pending_fix_review_ids", review_space_new),
        (
            "pending_fix_review_summary_max_id",
            "pending_fix_review_summary_ids",
            review_summary_new,
        ),
    ):
        if batch:
            state.set(max_key, max(feedback.id for feedback in batch))
            state.set(ids_key, [feedback.id for feedback in batch])
