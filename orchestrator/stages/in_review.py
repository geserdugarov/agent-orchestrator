# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""In-review stage handler and its PR-side primitives.

Owns `_handle_in_review` plus the in_review-private helpers: the
first-tick watermark migration (`_seed_legacy_in_review_watermarks`),
the cross-namespace watermark ratchet (`_bump_in_review_watermarks`),
and the debounce timestamp accessor (`_comment_created_at`).

The handler is permanently manual-merge-only: humans drive the merge.
Agent-approved + documented PR heads (or formally GitHub-approved
heads) that are mergeable and carry no standing human CHANGES_REQUESTED
get a one-shot HITL ping per head SHA; unmergeable PRs park awaiting
human attention; external merges/closes terminate the issue. The
orchestrator never calls `gh.merge_pr` from here, never routes to
`resolving_conflict` from a mergeability gate, and never emits
`merge_attempt` / orchestrator-initiated `pr_merged` events.

ALL workflow-owned helpers (`_park_awaiting_human`, `_handle_dev_fix_result`,
`_post_user_content_change_result`, `_resume_dev_with_text`, `_now_iso`,
the worktree plumbing, the drift / manifest / messaging helpers
re-exported into `workflow`) are reached through the parent module via
`from .. import workflow as _wf` at call time. The compatibility surface
tests rely on -- `patch.object(workflow, "_foo")` -- has to keep working
from inside the stage module too, so the handler must NOT direct-import
these names from `workflow_drift` / `workflow_messages` / `worktrees`;
doing so would bind a stable reference that test patches against
`workflow.X` could not affect.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from github.Issue import Issue

from .. import config
from ..config import RepoSpec
from ..github import (
    BASE_SYNC_HOLD_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)


def _comment_created_at(comment) -> Optional[datetime]:
    """Return a tz-aware UTC datetime for a comment, or None if unavailable.

    Real PyGithub `IssueComment.created_at` is always set, but the fakes used
    in tests can leave it None when the test doesn't care about debounce.
    PullRequestReview surfaces its timestamp as `submitted_at` rather than
    `created_at`, so the in_review debounce reads either. Naive datetimes are
    interpreted as UTC (PyGithub returns naive UTC).
    """
    ca = getattr(comment, "created_at", None)
    if ca is None:
        ca = getattr(comment, "submitted_at", None)
    if ca is None:
        return None
    if ca.tzinfo is None:
        return ca.replace(tzinfo=timezone.utc)
    return ca


def _bump_in_review_watermarks(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    issue_space_new: Optional[list] = None,
    review_space_new: Optional[list] = None,
    review_summary_new: Optional[list] = None,
) -> None:
    """Push the in_review watermarks past anything we've seen so far AND past
    any park comment we just wrote on the issue thread.

    Without this, a park-and-write at in_review (unmergeable PR, failed
    dev fix) leaves `pr_last_comment_id` lagging behind the orchestrator
    park message it just posted; the next tick scans the issue thread
    from the older watermark and routes the orchestrator's own HITL ping
    as fresh PR feedback to `fixing`. The ratchet is one-way (only ever
    increases) so callers can pass just-consumed comments or omit them
    and let `latest_comment_id` carry it.
    """
    candidates: list[int] = []
    cur_issue_wm = state.get("pr_last_comment_id")
    if isinstance(cur_issue_wm, int):
        candidates.append(cur_issue_wm)
    last_action = state.get("last_action_comment_id")
    if isinstance(last_action, int):
        candidates.append(last_action)
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        candidates.append(latest)
    if issue_space_new:
        candidates.extend(c.id for c in issue_space_new)
    if candidates:
        state.set("pr_last_comment_id", max(candidates))

    review_candidates: list[int] = []
    cur_review_wm = state.get("pr_last_review_comment_id")
    if isinstance(cur_review_wm, int):
        review_candidates.append(cur_review_wm)
    if review_space_new:
        review_candidates.extend(c.id for c in review_space_new)
    if review_candidates:
        state.set("pr_last_review_comment_id", max(review_candidates))

    summary_candidates: list[int] = []
    cur_summary_wm = state.get("pr_last_review_summary_id")
    if isinstance(cur_summary_wm, int):
        summary_candidates.append(cur_summary_wm)
    if review_summary_new:
        summary_candidates.extend(r.id for r in review_summary_new)
    if summary_candidates:
        state.set("pr_last_review_summary_id", max(summary_candidates))


def _seed_legacy_in_review_watermarks(
    gh: GitHubClient, issue: Issue, pr, state: PinnedState
) -> None:
    """First-tick migration: seed any missing in_review watermark past every
    comment currently visible on its surface, and record the seed in pinned
    state immediately.

    Issues that reached `in_review` before the validating handoff started
    seeding watermarks (or that were manually relabeled, or whose handoff
    failed to snapshot the PR) sit on `_handle_in_review` with
    `pr_last_comment_id`/`pr_last_review_comment_id`/`pr_last_review_summary_id`
    all unset. Without this seed, the next tick would call
    `comments_after(..., None)` on each surface and treat every historical
    comment -- including the orchestrator's own pickup / PR-opened / approval
    messages -- as fresh PR feedback once the debounce expires, routing the
    issue to `fixing` over its own historical messages.

    Tests that want to drive `_handle_in_review` against pre-existing comments
    seed the relevant watermark explicitly so this helper is a no-op for them.
    """
    # Each missing watermark is persisted on this tick -- 0 if the surface
    # currently has no content, otherwise the latest visible id. Persisting
    # 0 in the empty case is what stops the migration from re-firing on the
    # next tick: if we left the watermark unset, the FIRST human inline /
    # summary review added afterward would be consumed by a re-run of this
    # seed before `_handle_in_review` builds `new_comments`, so the fresh
    # feedback route would silently swallow that first review.
    seeded = False
    if (
        state.get("pr_last_comment_id") is None
        and state.get("last_action_comment_id") is None
    ):
        candidates: list[int] = []
        issue_latest = gh.latest_comment_id(issue)
        if isinstance(issue_latest, int):
            candidates.append(issue_latest)
        pr_conv = list(gh.pr_conversation_comments_after(pr, None))
        if pr_conv:
            candidates.append(max(c.id for c in pr_conv))
        state.set("pr_last_comment_id", max(candidates) if candidates else 0)
        seeded = True

    if state.get("pr_last_review_comment_id") is None:
        inline = list(gh.pr_inline_comments_after(pr, None))
        state.set(
            "pr_last_review_comment_id",
            max(c.id for c in inline) if inline else 0,
        )
        seeded = True

    if state.get("pr_last_review_summary_id") is None:
        summaries = list(gh.pr_reviews_after(pr, None))
        state.set(
            "pr_last_review_summary_id",
            max(r.id for r in summaries) if summaries else 0,
        )
        seeded = True

    if seeded:
        gh.write_pinned_state(issue, state)


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


def _handle_in_review(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
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
    HITL ping per head SHA so the human knows the PR is ready; an
    unmergeable PR parks awaiting human attention (no `resolving_conflict`
    route from this stage).

    User-content drift (a human edited the issue title/body while the PR
    was open) takes the dev-resume path here; both a pushed fix and a
    no-commit ACK bounce DIRECTLY back to `validating` (with
    `review_round` reset) so the reviewer re-evaluates against the
    updated body. Docs do not run on the drift exit: the single docs
    pass is deferred to the final-docs handoff after reviewer approval.
    """
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if pr_number is None:
        # Manual relabel from outside the validating path. We don't try to
        # infer the PR -- park once and let the human relabel back.
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
        return

    pr = gh.get_pr(int(pr_number))

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
        gh, spec, issue, state, pr, stage="in_review",
    ):
        return

    # Fresh PR feedback scan runs FIRST -- BEFORE the user-content drift
    # check below. `user_content_hash` covers title + body + every human
    # issue-thread comment, so without this ordering a normal fresh
    # issue-thread review comment would also flip the hash and the drift
    # path would resume the dev + bounce to `validating` instead of
    # recording `pending_fix_*` and flipping to `fixing`. That violates the
    # documented in_review -> fixing contract for issue-thread feedback.
    # Reorder so issue-thread / PR-conversation / inline-review /
    # review-summary feedback always routes to `fixing`, and the drift
    # check only fires for true title/body edits that the feedback scan
    # cannot represent.
    _seed_legacy_in_review_watermarks(gh, issue, pr, state)
    # `or` would discard a legacy default of `pr_last_comment_id == 0` and
    # fall back to `last_action_comment_id` (the id of a prior park
    # comment), which sits ABOVE any human "do not merge yet" comment
    # posted earlier during implementing/validating; that human comment
    # would then never surface as fresh PR feedback. Treat 0 as a valid
    # "scan from the beginning" watermark.
    issue_wm = state.get("pr_last_comment_id")
    if issue_wm is None:
        issue_wm = state.get("last_action_comment_id")
    review_wm = state.get("pr_last_review_comment_id")
    review_summary_wm = state.get("pr_last_review_summary_id")
    orchestrator_ids = _wf._orchestrator_ids(state)
    new_issue_side = [
        c for c in gh.comments_after(issue, issue_wm)
        if c.id not in orchestrator_ids
    ]
    new_pr_conv = [
        c for c in gh.pr_conversation_comments_after(pr, issue_wm)
        if c.id not in orchestrator_ids
    ]
    new_pr_inline = list(gh.pr_inline_comments_after(pr, review_wm))
    new_pr_reviews = list(gh.pr_reviews_after(pr, review_summary_wm))
    issue_space_new = sorted(
        list(new_issue_side) + list(new_pr_conv), key=lambda c: c.id
    )
    review_space_new = sorted(new_pr_inline, key=lambda c: c.id)
    review_summary_new = sorted(new_pr_reviews, key=lambda r: r.id)
    new_comments = issue_space_new + review_space_new + review_summary_new

    # If a previous tick already parked on an unrecoverable state and
    # nothing changed since, do nothing -- the human action that unsticks
    # us is a comment, a relabel, or closing/merging the PR. The first two
    # land in `new_comments`; the last two are caught by the `pr_status`
    # branches above.
    if state.get("awaiting_human") and not new_comments:
        return

    if new_comments:
        # Hand the fresh PR feedback off to the `fixing` stage instead of
        # silently waiting through the debounce window or spawning the dev
        # agent here. Recording the per-namespace high ids in pinned state
        # gives the fixing handler a bookmark of what triggered the route
        # so it can resume the dev session, push a fix, and flip back to
        # `validating` for re-review -- all without `_handle_in_review`
        # having to keep the comment-debounce / dev-resume machinery in
        # its own body.
        #
        # Deliberately NOT honoring the debounce window before the flip: the
        # in_review handler used to wait IN_REVIEW_DEBOUNCE_SECONDS so a
        # human typing follow-up comments would not get a half-quoted prompt
        # forwarded to the dev. With the route to `fixing`, the dev is no
        # longer spawned from this handler at all -- the fixing stage owns
        # debouncing before its own spawn, so flipping immediately here is
        # the right contract (the issue's `fixing` label surfaces the
        # transition to the operator straight away, and any concurrent
        # additional comments are seen by the fixing handler on its next
        # tick).
        #
        # Watermarks are deliberately NOT bumped here: the fixing handler
        # needs to read these same comments to build its dev-resume prompt,
        # so consuming them now would lose the triggering feedback. The
        # `pending_fix_*_max_id` keys are bookmarks (a hint for the future
        # handler / for observability), not watermarks.
        state.set("pending_fix_at", _wf._now_iso())
        if issue_space_new:
            state.set(
                "pending_fix_issue_max_id",
                max(c.id for c in issue_space_new),
            )
        if review_space_new:
            state.set(
                "pending_fix_review_max_id",
                max(c.id for c in review_space_new),
            )
        if review_summary_new:
            state.set(
                "pending_fix_review_summary_max_id",
                max(r.id for r in review_summary_new),
            )
        # Update `user_content_hash` so the user-content drift detection
        # below does NOT fire on the next tick for the same comment changes
        # we just consumed via the fixing route. The hash covers title +
        # body + human issue-thread comments, so any issue-thread comment
        # in `new_issue_side` shifts the hash; leaving the old hash in
        # pinned state would have the drift path resume the dev and bounce
        # back to `validating` the moment a human relabels the issue
        # back to `in_review`, undoing the fixing route.
        # `_compute_user_content_hash` is a pure read of the current
        # issue state, so reading it here is cheap and self-contained --
        # the bookmark and the hash both advance atomically with the
        # label flip.
        state.set(
            "user_content_hash",
            _wf._compute_user_content_hash(
                issue, _wf._orchestrator_ids(state),
            ),
        )
        # If we were parked awaiting human, the comment that triggered this
        # route is the human signal -- clear the park flags so the fixing
        # handler is not greeted with stale awaiting_human state.
        state.set("awaiting_human", False)
        state.set("park_reason", None)
        gh.set_workflow_label(issue, "fixing")
        gh.write_pinned_state(issue, state)
        return

    # User-content drift: a human edited the issue title/body after the PR
    # opened (no fresh comment surface triggered the fixing route above).
    # Notify on both surfaces, resume the dev session on its locked
    # backend with the new body, and on either outcome (pushed fix or
    # no-commit ACK) bounce DIRECTLY back to `validating` so the
    # reviewer re-evaluates against the updated body. Docs do not run
    # on the drift exit: the single docs pass is deferred to the
    # final-docs handoff after reviewer approval.
    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        state.set("user_content_hash", new_hash)
        # Gather unread PR-conversation comments BEFORE posting the
        # orchestrator's drift notice. The issue thread and PR
        # conversation share the IssueComment id space, so the
        # subsequent `_bump_in_review_watermarks` bump (driven by
        # `latest_comment_id(issue)` and `last_action_comment_id`,
        # both of which only reflect the ISSUE thread) can leap past
        # a PR-conversation comment whose id falls between the prior
        # `pr_last_comment_id` and the new issue-thread max -- the
        # dev would never see it. Capturing those PR comments here
        # and quoting them in the followup prompt is what stops a
        # concurrent PR comment from being silently dropped by the
        # watermark bump. Filtering by `orchestrator_comment_ids` and
        # `_ORCH_COMMENT_MARKER` mirrors the regular in_review
        # comment-driven scan; a PR comment whose id is recorded as
        # orchestrator-authored or whose body carries the marker is
        # not real feedback and stays out of the followup.
        issue_wm = state.get("pr_last_comment_id")
        if issue_wm is None:
            issue_wm = state.get("last_action_comment_id")
        orchestrator_ids = _wf._orchestrator_ids(state)
        unread_pr_conv = [
            c for c in gh.pr_conversation_comments_after(pr, issue_wm)
            if c.id not in orchestrator_ids
            and _wf._ORCH_COMMENT_MARKER not in (c.body or "")
        ]
        _wf._post_pr_comment(
            gh, int(pr_number), state,
            ":pencil2: issue body changed; resuming dev session.",
        )
        # Mark every issue-thread comment as consumed AND bump the
        # in_review watermarks past anything posted on this tick. The
        # dev sees the full thread via `_recent_comments_text` in the
        # resume prompt, so a later validating->in_review handoff
        # (after the "pushed" branch bounces straight to `validating`,
        # validating re-runs, and the reviewer approves) and the
        # in_review's own watermark check must not replay these
        # comments as fresh feedback.
        _wf._mark_drift_comments_consumed(gh, issue, state)
        wt = _wf._worktree_path(spec, issue.number)
        if not wt.exists():
            wt = _wf._ensure_worktree(spec, issue.number)
        before_sha = _wf._head_sha(wt)
        # Combine the issue-thread context with the unread PR-conversation
        # comments so the dev sees both surfaces before the watermark
        # bump below consumes them.
        comments_text = _wf._recent_comments_text(issue)
        if unread_pr_conv:
            pr_block = "\n\n".join(
                f"@{c.user.login if c.user else 'user'} (PR comment): "
                f"{c.body or ''}"
                for c in unread_pr_conv
            )
            prefix = f"{comments_text}\n\n" if comments_text else ""
            comments_text = (
                f"{prefix}Unread PR conversation comments:\n\n{pr_block}"
            )
        followup = _wf._build_user_content_change_prompt(issue, comments_text)
        wt, dev_result = _wf._resume_dev_with_text(
            gh, spec, issue, state, followup,
        )
        state.set("last_agent_action_at", _wf._now_iso())
        # The user-content-change result handler treats a no-commit reply
        # as an ack rather than parking on it; a harmless clarification
        # edit (the dev confirms the PR already satisfies it) must not
        # stall the issue with an "agent needs your input" park.
        outcome = _wf._post_user_content_change_result(
            gh, spec, issue, state, wt, dev_result, before_sha,
        )
        # Always bump in_review watermarks past the orchestrator's notice
        # and any comments we just consumed, regardless of outcome. The
        # next tick will be in `validating` on either successful outcome
        # ("pushed" or "ack"); if the reviewer later approves and
        # bounces back to in_review, `_seed_watermark_past_self` would
        # otherwise stop at the original human comment and trigger a
        # duplicate resume. Passing `unread_pr_conv` ensures
        # PR-conversation ids ABOVE the issue-thread max are also
        # included in the candidate set; without it, a PR comment with
        # id higher than every issue-thread id would survive past the
        # bump and re-fire as fresh feedback.
        _bump_in_review_watermarks(
            gh, issue, state, issue_space_new=unread_pr_conv,
        )
        if outcome in ("pushed", "ack"):
            # The drift invalidated the prior validation either way: the
            # reviewer agent approved against the OLD requirements, so
            # `review_round` must reset before the issue can earn a
            # fresh approval. Both outcomes bounce DIRECTLY back to
            # `validating` so the reviewer re-evaluates against the
            # updated body; docs do not run here (the single docs pass
            # is deferred to the final-docs handoff after reviewer
            # approval).
            state.set("review_round", 0)
            gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        _wf.log.info(
            "issue=#%d has %r; holding in_review HITL ping",
            issue.number, BASE_SYNC_HOLD_LABEL,
        )
        return

    # Manual-merge-only: humans drive the merge. An unmergeable PR parks
    # awaiting human regardless of approval state -- the orchestrator
    # never routes from here to `resolving_conflict` and never calls
    # `gh.merge_pr`. A mergeable PR earns a one-shot HITL ping per head
    # SHA when either the agent-approved final-docs handoff covers that
    # head OR GitHub carries a real APPROVED review on that head, and no
    # standing CHANGES_REQUESTED veto exists.
    mergeable = gh.pr_is_mergeable(pr)
    if mergeable is None:
        return  # GitHub still computing; try next tick
    if not mergeable:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} PR #{pr_number} is not mergeable "
            "(branch protection, conflicts, or out-of-date base); "
            "manual merge needed.",
            reason="unmergeable",
        )
        state.set("park_reason", "unmergeable")
        _bump_in_review_watermarks(gh, issue, state)
        gh.write_pinned_state(issue, state)
        return
    # mergeable: humans drive the merge. The ping advertises the PR as
    # "ready for review/merge", so it must only fire for a head the
    # orchestrator has reviewer-approved and documented (or one a
    # human/bot formally approved in GitHub) AND carries no standing
    # human veto; otherwise we would be inviting a manual merge over a
    # stale or rejected commit.
    head_sha = pr.head.sha
    if gh.pr_has_changes_requested(pr, head_sha=head_sha):
        return
    # Approval gate: the final-docs pass records the exact head it
    # checked after reviewer approval. If a later push changes the PR
    # head, the docs marker no longer matches and the issue must bounce
    # back through validating/documenting before it can ping again. A
    # real GitHub APPROVED review on the current head remains a valid
    # fallback for manually-driven review flows.
    final_docs_ready = _final_docs_handoff_completed_for_head(state, head_sha)
    github_approved = (
        False if final_docs_ready else gh.pr_is_approved(pr, head_sha=head_sha)
    )
    if not (final_docs_ready or github_approved):
        return
    # Ping HITL handles once per head SHA so the human knows the PR is
    # ready. De-duplication is keyed on `ready_ping_sha` (the head we
    # pinged for); a new commit pushed onto the branch shifts
    # pr.head.sha and re-pings, while repeated ticks on the same head
    # stay silent. Deliberately do NOT set `awaiting_human` -- the
    # handler must still react to PR comments / external merge / a
    # later unmergeable transition.
    #
    # Deliberately NOT calling `_bump_in_review_watermarks` here: that
    # helper reads `gh.latest_comment_id(issue)`, which could include
    # a human issue/PR-conversation comment that landed between the
    # earlier comment scan and this point. Bumping the watermark past
    # an unobserved human comment would silently swallow it -- the
    # next tick's `comments_after` would skip it and the dev would
    # never see the feedback. The ping is recorded in
    # `orchestrator_comment_ids` by `_post_issue_comment`, so the
    # next tick's id-set filter excludes it from `new_issue_side`
    # without needing the watermark to move; a concurrent human
    # comment naturally surfaces below the unchanged watermark.
    if state.get("ready_ping_sha") != head_sha:
        _wf._post_issue_comment(
            gh, issue, state,
            f":bell: {config.HITL_MENTIONS} PR #{pr_number} is ready "
            "for review/merge.",
        )
        state.set("ready_ping_sha", head_sha)
        gh.write_pinned_state(issue, state)
