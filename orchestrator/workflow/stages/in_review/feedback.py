# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The four surfaces a human can answer a finished PR on, read as one batch.

Issue thread, PR conversation, inline review, and review summary all count as
PR feedback here, and the first two share the IssueComment id namespace, so
they merge into a single stream under a single watermark. What has to be
stripped from that stream is everything the orchestrator itself said: by
recorded id AND by the hidden body marker, because the id ledger is capped and
evicts while the marker stays on the comment forever. Missing one is how the
stage reads its own HITL ping as human feedback and routes the issue to
`fixing` against it.

The trusted-author filter sits above all four surfaces rather than inside the
route, so an outsider commenting on a public PR cannot bookmark a pending fix
or steer the stage. An empty allowlist trusts everyone, so the default
deployment is unchanged.

`_stay_parked` is the one case where fresh comments are deliberately ignored.
A park filed by the base-sync retry loop owns the comment that answers it: the
human's "retry the rebase" nudge belongs to `_sync_pr_worktree_to_base`, and
consuming it here as PR feedback would silently drop the retry intent.
"""
from __future__ import annotations

from typing import Optional

from orchestrator.git.base_sync import state as _base_sync_state
from orchestrator.github.comments import filter_trusted
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import comments as _comments
from orchestrator.workflow.stages.in_review import fixing_route as _fixing_route
from orchestrator.workflow.stages.in_review import models as _models
from orchestrator.workflow.stages.in_review import state as _state
from orchestrator.workflow.stages.in_review import watermarks as _watermarks


def _drop_orchestrator_comments(comments, orchestrator_ids) -> list:
    """Keep only genuine human feedback from an issue-thread / PR-conversation
    comment stream.

    Issue-thread and PR-conversation comments share the IssueComment id
    namespace. Filter orchestrator comments by recorded id AND by the hidden
    body marker: older state can miss an id, and the bounded id list can
    eventually evict it, but the marker stays on the GitHub comment.
    """
    return [
        comment
        for comment in comments
        if comment.id not in orchestrator_ids
        and _comments._ORCH_COMMENT_MARKER not in (comment.body or "")
    ]


def _issue_side_watermark(state: PinnedState) -> Optional[int]:
    """Resolve the issue / PR-conversation scan watermark.

    `or` would discard a legacy default of `pr_last_comment_id == 0` and fall
    back to `last_action_comment_id` (the id of a prior park comment), which
    sits ABOVE any human "do not merge yet" comment posted earlier during
    implementing / validating; that human comment would then never surface as
    fresh PR feedback. Treat 0 as a valid "scan from the beginning" watermark.
    """
    issue_wm = state.get(_state._PR_LAST_COMMENT_ID)
    if issue_wm is None:
        issue_wm = state.get("last_action_comment_id")
    return issue_wm


def _fresh_issue_space(ctx: _models._InReviewContext, orchestrator_ids) -> list:
    """Merge fresh issue-thread and PR-conversation feedback -- one shared
    IssueComment id namespace -- into a single stream: drop orchestrator
    comments, drop untrusted authors, sort ascending by id. Filtering untrusted
    authors here keeps an outsider's issue / PR comment from bookmarking a
    pending fix or steering the `in_review` -> `fixing` route.
    """
    issue_wm = _issue_side_watermark(ctx.state)
    new_issue_side = _drop_orchestrator_comments(
        ctx.gh.comments_after(ctx.issue, issue_wm), orchestrator_ids,
    )
    new_pr_conv = _drop_orchestrator_comments(
        ctx.gh.pr_conversation_comments_after(ctx.pr, issue_wm), orchestrator_ids,
    )
    return filter_trusted(sorted(
        list(new_issue_side) + list(new_pr_conv),
        key=lambda comment: comment.id,
    ))


def _scan_fresh_pr_feedback(ctx: _models._InReviewContext):
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
    orchestrator_ids = _comments._orchestrator_ids(ctx.state)
    issue_space_new = _fresh_issue_space(ctx, orchestrator_ids)
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
    if not state.get("awaiting_human"):
        return False
    return (
        not new_comments
        or state.get("park_reason")
        in _base_sync_state._AUTO_REBASE_PARK_REASONS
    )


def _consume_fresh_feedback(ctx: _models._InReviewContext) -> bool:
    """Scan the four in_review surfaces and either stay silently parked or route
    fresh human feedback to `fixing`.

    Returns True when the tick is fully handled here (stayed parked or routed to
    `fixing`); False when no fresh feedback exists and the caller should fall
    through to the drift / mergeability gates.

    The scan runs FIRST -- BEFORE the user-content drift check -- because
    `user_content_hash` covers title + body + every human issue-thread comment,
    so without this ordering a normal issue-thread review comment would also
    flip the hash and the drift path would resume the dev + bounce to
    `validating` instead of recording `pending_fix_*` and flipping to `fixing`,
    violating the documented in_review -> fixing contract for issue-thread
    feedback.
    """
    _watermarks._seed_legacy_in_review_watermarks(ctx.gh, ctx.issue, ctx.pr, ctx.state)
    issue_space_new, review_space_new, review_summary_new = (
        _scan_fresh_pr_feedback(ctx)
    )
    new_comments = issue_space_new + review_space_new + review_summary_new
    if _stay_parked(ctx.state, new_comments):
        return True
    if not new_comments:
        return False
    _fixing_route._route_feedback_to_fixing(
        ctx, issue_space_new, review_space_new, review_summary_new,
    )
    return True
