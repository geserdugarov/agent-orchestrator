# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""In review handler."""
from __future__ import annotations

from orchestrator.stages import in_review as _owner

_InReviewContext = _owner._InReviewContext
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
config = _owner.config


def _head_is_approved(ctx: _InReviewContext, head_sha: str) -> bool:
    """True when `head_sha` earned the reviewer-approved final-docs handoff or
    carries a real GitHub APPROVED review.

    The final-docs pass records the exact head it checked after reviewer
    approval; if a later push changes the PR head, the docs marker no longer
    matches and the issue must bounce back through validating/documenting before
    it can ping again. A real GitHub APPROVED review on the current head is the
    fallback for manually-driven review flows -- probed only when the final-docs
    marker did not already qualify the head, to avoid a redundant API call.
    """
    if _owner._final_docs_handoff_completed_for_head(ctx.state, head_sha):
        return True
    return ctx.gh.pr_is_approved(ctx.pr, head_sha=head_sha)


def _handle_mergeable_gate(ctx: _InReviewContext) -> None:
    """Manual-merge-only mergeability gate. An unmergeable PR parks awaiting
    human regardless of approval state -- the orchestrator never routes from
    here to `resolving_conflict` and never calls `gh.merge_pr`. A mergeable PR
    earns a one-shot HITL ping per head SHA when either the agent-approved
    final-docs handoff covers that head OR GitHub carries a real APPROVED
    review on that head, and no standing CHANGES_REQUESTED veto exists.
    """
    from orchestrator import workflow as _wf

    pr = ctx.pr
    pr_number = ctx.pr_number
    mergeable = ctx.gh.pr_is_mergeable(pr)
    if mergeable is None:
        return  # GitHub still computing; try next tick
    if not mergeable:
        _wf._park_awaiting_human(
            ctx.gh, ctx.issue, ctx.state,
            f"{config.HITL_MENTIONS} PR #{pr_number} is not mergeable "
            "(branch protection, conflicts, or out-of-date base); "
            "manual merge needed.",
            reason="unmergeable",
        )
        ctx.state.set("park_reason", "unmergeable")
        _owner._bump_in_review_watermarks(ctx)
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)
        return
    # mergeable: humans drive the merge. The ping advertises the PR as "ready
    # for review/merge", so it must only fire for a head the orchestrator has
    # reviewer-approved and documented (or one a human/bot formally approved in
    # GitHub) AND carrying no standing human veto; otherwise we would invite a
    # manual merge over a stale or rejected commit.
    head_sha = pr.head.sha
    if ctx.gh.pr_has_changes_requested(pr, head_sha=head_sha):
        return
    if not _owner._head_is_approved(ctx, head_sha):
        return
    # Ping HITL handles once per head SHA so the human knows the PR is ready.
    # De-duplication is keyed on `ready_ping_sha` (the head we pinged for); a
    # new commit pushed onto the branch shifts pr.head.sha and re-pings, while
    # repeated ticks on the same head stay silent. Deliberately do NOT set
    # `awaiting_human` -- the handler must still react to PR comments / external
    # merge / a later unmergeable transition.
    #
    # Deliberately NOT calling `_bump_in_review_watermarks` here: that helper
    # reads `gh.latest_comment_id(issue)`, which could include a human
    # issue/PR-conversation comment that landed between the earlier comment scan
    # and this point. Bumping the watermark past an unobserved human comment
    # would silently swallow it -- the next tick's `comments_after` would skip
    # it and the dev would never see the feedback. The ping is recorded in
    # `orchestrator_comment_ids` by `_post_issue_comment`, so the next tick's
    # id-set filter excludes it without needing the watermark to move; a
    # concurrent human comment naturally surfaces below the unchanged watermark.
    if ctx.state.get("ready_ping_sha") != head_sha:
        _wf._post_issue_comment(
            ctx.gh, ctx.issue, ctx.state,
            f":bell: {config.HITL_MENTIONS} PR #{pr_number} is ready "
            "for review/merge.",
        )
        ctx.state.set("ready_ping_sha", head_sha)
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _consume_fresh_feedback(ctx: _InReviewContext) -> bool:
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
    _owner._seed_legacy_in_review_watermarks(ctx.gh, ctx.issue, ctx.pr, ctx.state)
    issue_space_new, review_space_new, review_summary_new = (
        _owner._scan_fresh_pr_feedback(ctx)
    )
    new_comments = issue_space_new + review_space_new + review_summary_new
    if _owner._stay_parked(ctx.state, new_comments):
        return True
    if not new_comments:
        return False
    _owner._route_feedback_to_fixing(
        ctx, issue_space_new, review_space_new, review_summary_new,
    )
    return True


def _park_missing_pr_number(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Park a manually-relabeled in_review issue that has no pinned `pr_number`.
    We don't infer the PR -- park once and let the human relabel back.
    """
    from orchestrator import workflow as _wf

    if state.get("awaiting_human"):
        return
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `in_review` without a pinned `pr_number`; "
        "manual relabeling suspected. Set the workflow label back to "
        "`validating` (or `implementing`) after fixing.",
        reason="missing_pr_number",
    )
    gh.write_pinned_state(issue, state)


def _handle_in_review(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    """Drive an in_review issue toward done / rejected, or hand fresh PR
    feedback off to the `fixing` stage.

    The handler always re-checks PR state (merged/closed) first so an external
    human merge wins over any orchestrator-side logic. Fresh actionable PR
    feedback on any of the four surfaces (issue thread, PR conversation,
    inline review, review summary) records pending-fix metadata in pinned
    state and flips the label to `fixing` immediately -- the dev resume and
    hand-back-to-`validating` cycle moves to the `fixing` handler. The
    orchestrator never merges from here: humans drive the merge. A
    mergeable PR whose current head completed the reviewer-approved
    final-docs handoff (or carries a real GitHub APPROVED review), with
    no standing human CHANGES_REQUESTED on that head, earns a one-shot
    HITL ping per head SHA so the human knows the PR is ready. An
    unmergeable PR parks awaiting human attention (no `resolving_conflict`
    route from this stage).

    User-content drift (a human edited the issue title/body while the PR
    was open) takes the dev-resume path here; both a pushed fix and a
    no-commit ACK bounce DIRECTLY back to `validating` (with
    `review_round` reset) so the reviewer re-evaluates against the
    updated body. Docs do not run on the drift exit: the single docs
    pass is deferred to the final-docs handoff after reviewer approval.
    """
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if pr_number is None:
        # Manual relabel from outside the validating path.
        _owner._park_missing_pr_number(gh, issue, state)
        return

    ctx = _InReviewContext(
        gh, spec, issue, state, gh.get_pr(int(pr_number)), pr_number,
    )

    # Drain the shared PR/issue terminal arcs (merged PR -> `done`,
    # closed PR -> `rejected`, open PR + manually-closed issue ->
    # `rejected` without branch cleanup). The closed-with-merged-PR
    # path (Resolves #N auto-close) is handled by the merged branch
    # inside the helper, so the open-PR + closed-issue arc only fires
    # for issues a human closed directly.
    #
    # Caveat carried over from the inline version: once the helper
    # flips a manually-closed (but PR-still-open) issue to `rejected`,
    # the dispatcher's terminal-label branch is a no-op AND
    # `list_pollable_issues` only sweeps closed issues still labeled
    # `in_review` / `resolving_conflict`. A later PR close is never
    # observed by the orchestrator, so the operator must clean up the
    # worktree, local branch, and remote branch manually for the
    # "close issue first, then close PR" ordering.
    if _wf._drain_review_pr_terminals(
        gh, spec, issue, state, ctx.pr, stage="in_review",
    ):
        return

    if _owner._consume_fresh_feedback(ctx):
        return

    if _owner._handle_user_content_drift(ctx):
        return

    _owner._handle_mergeable_gate(ctx)
