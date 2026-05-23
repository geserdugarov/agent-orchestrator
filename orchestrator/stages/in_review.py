# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""In-review stage handler and its PR-side primitives.

Owns `_handle_in_review` plus the in_review-private helpers: the transient
park-reason set (`_TRANSIENT_PARK_REASONS`), the quiet auto-merge gate
re-check (`_auto_merge_gates_pass`), the first-tick watermark migration
(`_seed_legacy_in_review_watermarks`), the cross-namespace watermark
ratchet (`_bump_in_review_watermarks`), and the debounce timestamp
accessor (`_comment_created_at`).

`_handle_resolving_conflict` still lives in `workflow.py` -- the
in_review handler only routes TO it (via the workflow label flip) and
does not import it directly.

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


# Park reasons that auto-resolve when the underlying GitHub state changes
# (CI rerun goes green, rebase resolves a conflict, branch protection drops
# a stale required review). Other parks (`missing_pr_number`, dev-fix
# failures) need explicit human action to unstick.
_TRANSIENT_PARK_REASONS = frozenset({"failed_checks", "unmergeable"})


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

    Without this, a park-and-write at in_review (failed checks, unmergeable,
    failed dev fix) leaves `pr_last_comment_id` lagging behind the orchestrator
    park message it just posted; the next tick scans the issue thread from the
    older watermark and resumes the dev agent on the orchestrator's own HITL
    ping. The ratchet is one-way (only ever increases) so callers can pass
    just-consumed comments or omit them and let `latest_comment_id` carry it.
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


def _auto_merge_gates_pass(
    gh: GitHubClient, pr, state: PinnedState
) -> bool:
    """All conditions required for auto-merge, evaluated quietly (no parking,
    no PR comments, no state writes).

    Used by the transient-park recovery path: when an awaiting_human issue
    re-enters `_handle_in_review` with no new comments, we want to detect a
    silently-resolved condition (CI now green, rebase made the PR mergeable)
    and unstick the merge without re-posting the park message every tick.
    Mirrors the inline gate sequence in `_handle_in_review` exactly so the
    two cannot drift.
    """
    head_sha = pr.head.sha
    if gh.pr_has_changes_requested(pr, head_sha=head_sha):
        return False
    approved_for_head = (
        state.get("agent_approved_sha") == head_sha
        or gh.pr_is_approved(pr, head_sha=head_sha)
    )
    if not approved_for_head:
        return False
    mergeable = gh.pr_is_mergeable(pr)
    if mergeable is None or not mergeable:
        return False
    return gh.pr_combined_check_state(pr) == "success"


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
    messages -- as fresh PR feedback once the debounce expires, resuming the
    dev and bouncing the PR back to validating even with `AUTO_MERGE` off.

    Tests that want to drive `_handle_in_review` against pre-existing comments
    seed the relevant watermark explicitly so this helper is a no-op for them.
    """
    # Each missing watermark is persisted on this tick -- 0 if the surface
    # currently has no content, otherwise the latest visible id. Persisting
    # 0 in the empty case is what stops the migration from re-firing on the
    # next tick: if we left the watermark unset, the FIRST human inline /
    # summary review added afterward would be consumed by a re-run of this
    # seed before `_handle_in_review` builds `new_comments`, so AUTO_MERGE
    # could land the PR over that first review.
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


def _handle_in_review(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """Drive an in_review issue toward done / rejected, or back to validating
    on a new PR comment.

    The handler always re-checks PR state (merged/closed) first so an external
    human merge wins over any orchestrator-side logic. A PR comment newer than
    the debounce window resumes the dev's locked-backend session and bounces
    the issue back to `validating` so the reviewer agent re-runs on the fix.
    Auto-merge is gated by `AUTO_MERGE` (default off); without it, the loop
    only handles state transitions and comment-driven re-fixes -- humans still
    click Merge.
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
    pr_status = gh.pr_state(pr)

    if pr_status == "merged":
        state.set("merged_at", _wf._now_iso())
        gh.set_workflow_label(issue, "done")
        gh.write_pinned_state(issue, state)
        gh.emit_event(
            "pr_merged",
            issue_number=issue.number,
            stage="in_review",
            pr_number=int(pr_number),
            sha=getattr(pr.head, "sha", None) or None,
            merge_method="external",
            review_round=int(state.get("review_round") or 0),
            conflict_round=state.get("conflict_round"),
            retry_count=state.get("retry_count"),
        )
        try:
            issue.edit(state="closed")
        except Exception:
            _wf.log.exception(
                "issue=#%s could not close after merge", issue.number,
            )
        _wf._cleanup_terminal_branch(gh, spec, issue.number)
        return

    if pr_status == "closed":  # closed without merge
        state.set("closed_without_merge_at", _wf._now_iso())
        gh.set_workflow_label(issue, "rejected")
        gh.write_pinned_state(issue, state)
        gh.emit_event(
            "pr_closed_without_merge",
            issue_number=issue.number,
            stage="in_review",
            pr_number=int(pr_number),
            sha=getattr(pr.head, "sha", None) or None,
            review_round=int(state.get("review_round") or 0),
            conflict_round=state.get("conflict_round"),
            retry_count=state.get("retry_count"),
        )
        try:
            issue.edit(state="closed")
        except Exception:
            _wf.log.exception(
                "issue=#%s could not close after reject", issue.number,
            )
        # The PR is gone, so the orchestrator-owned branch and worktree
        # are dead weight. Mirrors the merged-PR cleanup order: finalize
        # GitHub state first, then tidy local + remote refs best-effort.
        _wf._cleanup_terminal_branch(gh, spec, issue.number)
        return

    # PR is open BUT the issue was closed manually (the closed-in_review sweep
    # in `list_pollable_issues` yielded it). Closing the issue while its PR
    # is still open is a human stop signal -- without this branch, AUTO_MERGE
    # could otherwise land the PR and flip the issue to `done` over the
    # human's rejection. The closed-with-merged-PR path (Resolves #N
    # auto-close) is already handled by the `pr_status == "merged"` branch
    # above, so by the time we reach here a closed issue means the human
    # closed it directly.
    #
    # Deliberately NOT cleaning the branch here: the PR is still open and
    # the operator may want to inspect, salvage commits, transfer, or
    # reopen it. Deleting the branch would make the PR harder to review.
    #
    # Automatic cleanup-on-PR-close only happens if the PR is closed
    # BEFORE this handler flips the issue to `rejected`. Once the label
    # is `rejected` the dispatcher (workflow.py terminal-label branch)
    # is a no-op AND `list_pollable_issues` only sweeps closed issues
    # still labeled `in_review` / `resolving_conflict`, so a later PR
    # close is never observed by the orchestrator. The operator must
    # clean up the worktree, local branch, and remote branch manually
    # for the "close issue first, then close PR" ordering.
    if getattr(issue, "state", "open") == "closed":
        state.set("closed_without_merge_at", _wf._now_iso())
        gh.set_workflow_label(issue, "rejected")
        gh.write_pinned_state(issue, state)
        # Deliberately no `pr_closed_without_merge` emit here: the PR is
        # still open and may be reopened / salvaged. That event is reserved
        # for the actual closed-PR rejection arc above.
        return

    # User-content drift: a human edited the issue title/body after the PR
    # opened. Notify on both surfaces, resume the dev session on its locked
    # backend with the new body, and on a successful pushed fix bounce back
    # to `validating` so the reviewer agent re-runs on the new diff next
    # tick (mirrors the comment-driven dev-resume path below).
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
        # resume prompt, so a later validating->in_review handoff (after
        # the "pushed" branch flips to validating and the reviewer
        # approves) and the in_review's own watermark check must not
        # replay these comments as fresh feedback.
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
        # and any comments we just consumed, regardless of outcome. On
        # "pushed" the next tick will be in validating, but if the
        # reviewer later approves and bounces back to in_review,
        # `_seed_watermark_past_self` would otherwise stop at the
        # original human comment and trigger a duplicate resume. Passing
        # `unread_pr_conv` ensures PR-conversation ids ABOVE the
        # issue-thread max are also included in the candidate set;
        # without it, a PR comment with id higher than every issue-thread
        # id would survive past the bump and re-fire as fresh feedback.
        _bump_in_review_watermarks(
            gh, issue, state, issue_space_new=unread_pr_conv,
        )
        if outcome in ("pushed", "ack"):
            # The drift invalidated the prior validation: the reviewer
            # agent approved against the OLD requirements, so its
            # `agent_approved_sha` snapshot is stale. Bounce back to
            # `validating` (reset `review_round=0`) so the reviewer
            # re-evaluates against the updated body/comments before
            # AUTO_MERGE is allowed to land the PR.
            #
            # An "ack" outcome (no commit; dev said the existing work
            # already satisfies the edit) MUST also bounce: leaving the
            # issue at `in_review` with the old `agent_approved_sha`
            # would let the AUTO_MERGE gate pass on the next tick even
            # though the reviewer never saw the changed requirements.
            # Clear `agent_approved_sha` defensively in case any future
            # auto-merge path reads it before the reviewer re-snapshots.
            state.set("review_round", 0)
            state.set("agent_approved_sha", None)
            gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    # PR is open. Look for new human activity. Three watermarks because the
    # three comment surfaces live in distinct id namespaces in GitHub's REST
    # API: issue/PR-conversation comments share the IssueComment id space,
    # inline review comments live in the PullRequestComment id space, and
    # PR review summaries (the body posted alongside an APPROVE / REQUEST
    # CHANGES / COMMENT submission) live in the PullRequestReview id space.
    # Mixing any two under one int would silently drop or replay one side.
    # Orchestrator-authored items are filtered by exact id (recorded when
    # we posted them); we cannot key this on author login because a PAT
    # shared with a human reviewer's GitHub account is a normal deployment
    # shape, and login-matching would silently drop that human's feedback.
    # The id-set filter is restricted to the IssueComment namespace -- the
    # only surface the orchestrator posts on -- so a human inline review
    # comment or PR review summary that happens to share a numeric id with
    # a recorded bot comment is not falsely dropped.
    _seed_legacy_in_review_watermarks(gh, issue, pr, state)
    # `or` would discard a legacy default of `pr_last_comment_id == 0` and
    # fall back to `last_action_comment_id` (the id of a prior park
    # comment), which sits ABOVE any human "do not merge yet" comment
    # posted earlier during implementing/validating; that human comment
    # would then never surface and AUTO_MERGE could land the PR over it.
    # Treat 0 as a valid "scan from the beginning" watermark.
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
    #
    # Exception: when the park reason is transient (failed checks or PR not
    # yet mergeable) and `AUTO_MERGE` is on, re-evaluate the gates here. A
    # human who reruns CI green or rebases the branch without leaving a
    # comment would otherwise leave the issue stuck in_review forever.
    if state.get("awaiting_human") and not new_comments:
        if not (
            config.AUTO_MERGE
            and state.get("park_reason") in _TRANSIENT_PARK_REASONS
        ):
            return
        if not _auto_merge_gates_pass(gh, pr, state):
            return  # still stuck, do not re-post the park comment
        # Conditions resolved: clear the park flags and fall through to the
        # auto-merge block, which re-checks the same gates and merges.
        state.set("awaiting_human", False)
        state.set("park_reason", None)

    if new_comments:
        timestamps = [
            ts for ts in (_comment_created_at(c) for c in new_comments)
            if ts is not None
        ]
        if timestamps:
            newest_ts = max(timestamps)
            elapsed = (datetime.now(timezone.utc) - newest_ts).total_seconds()
            if elapsed < config.IN_REVIEW_DEBOUNCE_SECONDS:
                return  # human may still be typing; wait a tick

        followup = _wf._build_pr_comment_followup(new_comments)
        wt = _wf._worktree_path(spec, issue.number)
        if not wt.exists():
            wt = _wf._ensure_worktree(spec, issue.number)
        before_sha = _wf._head_sha(wt)
        wt, dev_result = _wf._resume_dev_with_text(
            gh, spec, issue, state, followup,
        )
        state.set("last_agent_action_at", _wf._now_iso())
        if not _wf._handle_dev_fix_result(
            gh, spec, issue, state, wt, dev_result, before_sha
        ):
            # Park has updated last_action_comment_id; bump the in_review
            # watermarks past anything we just consumed so the next tick does
            # not replay these comments OR the orchestrator's own park
            # message as fresh PR feedback.
            _bump_in_review_watermarks(
                gh, issue, state,
                issue_space_new=issue_space_new,
                review_space_new=review_space_new,
                review_summary_new=review_summary_new,
            )
            gh.write_pinned_state(issue, state)
            return
        # Successful fix pushed -- bounce back to validating so the reviewer
        # re-runs on the next tick. Reset round counter; this is a new diff.
        if issue_space_new:
            state.set(
                "pr_last_comment_id", max(c.id for c in issue_space_new)
            )
        if review_space_new:
            state.set(
                "pr_last_review_comment_id",
                max(c.id for c in review_space_new),
            )
        if review_summary_new:
            state.set(
                "pr_last_review_summary_id",
                max(r.id for r in review_summary_new),
            )
        state.set("review_round", 0)
        gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        _wf.log.info(
            "issue=#%d has %r; holding in_review auto-merge/base-sync gates",
            issue.number, BASE_SYNC_HOLD_LABEL,
        )
        return

    # No new comments. The two AUTO_MERGE branches diverge on what
    # "unmergeable" means:
    #
    #   * AUTO_MERGE off: legacy fallback path. Humans drive the merge,
    #     so an unmergeable PR just needs visibility -- park awaiting
    #     human regardless of approval state. Unauthenticated humans
    #     re-approve as part of fixing the unmergeable state, so gating
    #     the legacy park on approval would silently hide the unmergeable
    #     condition for any PR that hasn't been re-approved yet.
    #
    #   * AUTO_MERGE on: the resolving_conflict route REPLACES the old
    #     unmergeable park, which only fired AFTER the changes-requested
    #     and approval gates. Resuming dev work for an unapproved PR (or
    #     one carrying a standing human CHANGES_REQUESTED) would push
    #     unreviewed work past a human veto, so the gates run first and
    #     the unmergeable check is gated behind them.
    if not config.AUTO_MERGE:
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
        return  # mergeable: humans drive the merge from here

    # AUTO_MERGE on. Run the original gating order: changes-requested
    # and approval first, then mergeable. This matches the pre-rollout
    # behavior and ensures the resolving_conflict route only fires for
    # PRs that would have hit the old unmergeable park.
    head_sha = pr.head.sha
    # A human CHANGES_REQUESTED on the current head vetoes auto-merge
    # regardless of how the reviewer agent voted. Without this check, the
    # `agent_approved_sha == head_sha` short-circuit below would let the
    # orchestrator merge over a standing human objection on the same SHA.
    if gh.pr_has_changes_requested(pr, head_sha=head_sha):
        return
    # Approval can come from either side: the reviewer agent persists
    # `agent_approved_sha` for the head it OK'd (the agent posts an issue
    # comment, not a real PR review, so pr_is_approved alone would never be
    # True for the agent flow), OR a human/bot submitted a real APPROVED
    # review on the *current* head SHA. Stale human approvals on older
    # commits do NOT count -- a commit pushed after a human approval must
    # not auto-merge unless the human re-approves.
    approved_for_head = (
        state.get("agent_approved_sha") == head_sha
        or gh.pr_is_approved(pr, head_sha=head_sha)
    )
    if not approved_for_head:
        return
    mergeable = gh.pr_is_mergeable(pr)
    if mergeable is None:
        return  # GitHub still computing; try next tick
    if pr.head.sha != head_sha:
        # `pr_is_mergeable` calls `pr.update()` to resolve a `None`
        # mergeable, which refreshes `pr.head.sha`. The approval and
        # changes-requested gates above ran against the earlier head_sha,
        # so a commit landing during the refresh would otherwise let the
        # subsequent failed-checks branch park on the WRONG sha or, worse,
        # let an unreviewed head reach the merge call. Bail and re-evaluate
        # all gates against the new head on the next tick.
        return
    if not mergeable:
        # Approved + no human veto + still unmergeable: route to
        # `resolving_conflict` for an automated merge-of-base /
        # conflict-resolve attempt. PyGithub does not distinguish a
        # content conflict from branch-protection / out-of-date-base
        # on `mergeable=False`; we treat any unmergeable PR here as
        # eligible. If the underlying cause is branch protection, the
        # merge attempt is a no-op fast-forward and the `conflict_round`
        # counter still ticks (so a perpetually-unmergeable PR cannot
        # loop in_review <-> resolving_conflict forever).
        #
        # Initialize `conflict_round` only when absent: a PR that bounces
        # back to `in_review` with the counter already set must NOT
        # reset it -- the cap would be ineffective if every re-entry
        # zeroed the counter, and a branch-protection-only PR would
        # ping-pong between handlers indefinitely.
        if state.get("conflict_round") is None:
            state.set("conflict_round", 0)
        try:
            _wf._post_pr_comment(
                gh, int(pr_number), state,
                f":mag: PR is not mergeable; orchestrator is attempting "
                f"auto-resolution by merging "
                f"`{spec.remote_name}/{spec.base_branch}` "
                "into the branch (label: `resolving_conflict`).",
            )
        except Exception:
            _wf.log.exception(
                "issue=#%s could not post conflict-resolution notice to "
                "PR #%s", issue.number, pr_number,
            )
        _bump_in_review_watermarks(gh, issue, state)
        gh.emit_event(
            "conflict_round",
            issue_number=issue.number,
            stage="in_review",
            pr_number=int(pr_number),
            sha=head_sha,
            action="entered",
            conflict_round=int(state.get("conflict_round") or 0),
            review_round=int(state.get("review_round") or 0),
            retry_count=state.get("retry_count"),
        )
        gh.set_workflow_label(issue, "resolving_conflict")
        gh.write_pinned_state(issue, state)
        return
    check = gh.pr_combined_check_state(pr)
    if check == "pending":
        return
    if check in ("failure", "none"):
        # 'none' means no checks at all -- ambiguous, refuse to merge.
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} PR #{pr_number} checks are {check!r}; "
            "refusing to auto-merge.",
            reason="failed_checks",
        )
        state.set("park_reason", "failed_checks")
        _bump_in_review_watermarks(gh, issue, state)
        gh.write_pinned_state(issue, state)
        return

    # Approved + mergeable + green: SHA-pinned merge to the head we GATED
    # on, NOT the (possibly-refreshed) `pr.head.sha`. `pr_is_mergeable`
    # may have refreshed `pr.head.sha` above; using that value here would
    # let a commit landing during the refresh slip through past the
    # approval and changes-requested gates. The SHA-shift check above
    # already bails when this happens, but pinning to `head_sha` here is
    # belt-and-suspenders: GitHub returns 409 for a SHA mismatch so a
    # missed shift cannot merge an unreviewed head.
    merged_ok = gh.merge_pr(pr, sha=head_sha)
    gh.emit_event(
        "merge_attempt",
        issue_number=issue.number,
        stage="in_review",
        pr_number=int(pr_number),
        sha=head_sha,
        method="squash",
        result="success" if merged_ok else "failed",
        check_state=check,
        review_round=int(state.get("review_round") or 0),
        conflict_round=state.get("conflict_round"),
        retry_count=state.get("retry_count"),
    )
    if not merged_ok:
        # 405/409/422 -- next tick will re-evaluate; if it still won't merge,
        # the GH UI shows why.
        return
    state.set("merged_at", _wf._now_iso())
    gh.set_workflow_label(issue, "done")
    gh.write_pinned_state(issue, state)
    gh.emit_event(
        "pr_merged",
        issue_number=issue.number,
        stage="in_review",
        pr_number=int(pr_number),
        sha=head_sha,
        merge_method="squash",
        check_state=check,
        review_round=int(state.get("review_round") or 0),
        conflict_round=state.get("conflict_round"),
        retry_count=state.get("retry_count"),
    )
    try:
        issue.edit(state="closed")
    except Exception:
        _wf.log.exception(
            "issue=#%s could not close after auto-merge", issue.number,
        )
    _wf._cleanup_terminal_branch(gh, spec, issue.number)
