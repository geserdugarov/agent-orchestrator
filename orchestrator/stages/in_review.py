# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""In-review stage handler and its PR-side primitives.

Owns `_handle_in_review` and the per-tick `_InReviewContext` bundle threaded
through its sub-handlers: the fresh-feedback consumer (`_consume_fresh_feedback`,
`_scan_fresh_pr_feedback`, `_fresh_issue_space`) and its filter / watermark
primitives (`_drop_orchestrator_comments`, `_issue_side_watermark`,
`_bump_in_review_watermarks`), the fixing-route recorder
(`_route_feedback_to_fixing`, `_record_pending_fix_bookmarks`), the
user-content-drift path (`_handle_user_content_drift` and its
`_drift_unread_pr_conv` / `_drift_worktree` / `_resume_dev_for_drift` /
`_dispose_drift_result` steps around the `_DriftResume` bundle, plus
`_build_drift_resume_prompt`), the manual-merge mergeability gate
(`_handle_mergeable_gate`, `_head_is_approved`,
`_final_docs_handoff_completed_for_head`), the missing-PR park
(`_park_missing_pr_number`), the parked-tick guard (`_stay_parked`), the
first-tick watermark migration (`_seed_legacy_in_review_watermarks` /
`_seed_missing_watermark`), and the debounce timestamp accessor
(`_comment_created_at`).

The handler is permanently manual-merge-only: humans drive the merge.
Agent-approved + documented PR heads (or formally GitHub-approved
heads) that are mergeable and carry no standing human CHANGES_REQUESTED
get a one-shot HITL ping per head SHA; a `quick_run` issue is exempt
from the approval markers and earns the ping on any mergeable head with
no standing CHANGES_REQUESTED; unmergeable PRs park awaiting human
attention; external merges/closes terminate the issue. The
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

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator.comment_trust import filter_trusted
from orchestrator.config import RepoSpec
from orchestrator.state_machine import WorkflowLabel
from orchestrator.github import (
    QUICK_RUN_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)


# Pinned-state key tracking the newest PR comment already folded into state.
_PR_LAST_COMMENT_ID = "pr_last_comment_id"


@dataclass(frozen=True)
class _InReviewContext:
    """The per-tick `in_review` invocation handles, bundled so the fresh-feedback
    scan, fixing-route, drift, and mergeability sub-handlers thread them as a
    single value instead of five/six positional arguments (mirrors fixing's
    `_FixingContext`). `pr` is the live PR fetched this tick; `pr_number` is the
    pinned PR number `_handle_in_review` already validated as present.
    """
    gh: GitHubClient
    spec: RepoSpec
    issue: Issue
    state: PinnedState
    pr: Any
    pr_number: Any


@dataclass(frozen=True)
class _DriftResume:
    """Outcome of the drift dev-resume: the (possibly recreated) worktree, the
    agent result, whether an operator paused mid-run, and the pre-resume HEAD
    used to tell a pushed fix from a no-commit ack.
    """
    worktree: Any
    dev_result: Any
    paused: bool
    before_sha: Any


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
    ctx: _InReviewContext, *, issue_space_new: Optional[list] = None,
) -> None:
    """Push the in_review issue-side watermark (`pr_last_comment_id`) past
    everything seen so far AND past any park comment just written on the issue
    thread.

    Without this, a park-and-write at in_review (unmergeable PR, failed dev fix)
    leaves `pr_last_comment_id` lagging behind the orchestrator park message it
    just posted; the next tick scans the issue thread from the older watermark
    and routes the orchestrator's own HITL ping as fresh PR feedback to
    `fixing`. The ratchet is one-way (only ever increases), so callers pass
    just-consumed comments or omit them and let `latest_comment_id` carry it.

    Only the issue-side watermark moves here. The inline-review and
    review-summary watermarks belong to the `fixing` handler, which advances
    them when it consumes that feedback; in_review never consumes review-surface
    comments itself (it routes them to `fixing`), so there is nothing to ratchet
    past on those surfaces.
    """
    candidates: list[int] = []
    cur_issue_wm = ctx.state.get(_PR_LAST_COMMENT_ID)
    if isinstance(cur_issue_wm, int):
        candidates.append(cur_issue_wm)
    last_action = ctx.state.get("last_action_comment_id")
    if isinstance(last_action, int):
        candidates.append(last_action)
    latest = ctx.gh.latest_comment_id(ctx.issue)
    if isinstance(latest, int):
        candidates.append(latest)
    if issue_space_new:
        candidates.extend(comment.id for comment in issue_space_new)
    if candidates:
        ctx.state.set(_PR_LAST_COMMENT_ID, max(candidates))


def _seed_missing_watermark(state: PinnedState, key: str, fetch) -> bool:
    """Seed a single missing review-surface watermark past the latest id
    currently visible on that surface, or 0 when it is empty. Returns whether
    a seed was written (so the caller knows a persist is needed).

    `fetch` is a thunk so the surface is only queried when `key` is unset --
    an already-seeded watermark must not trigger a redundant GitHub read.
    Persisting 0 for an empty surface (rather than leaving the key unset) is
    what stops the migration from re-firing next tick and swallowing the
    first human review added in between; see `_seed_legacy_in_review_watermarks`.
    """
    if state.get(key) is not None:
        return False
    surface_comments = list(fetch())
    state.set(
        key,
        max(comment.id for comment in surface_comments)
        if surface_comments
        else 0,
    )
    return True


def _seed_legacy_in_review_watermarks(
    gh: GitHubClient, issue: Issue, pr, state: PinnedState,
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
        state.get(_PR_LAST_COMMENT_ID) is None
        and state.get("last_action_comment_id") is None
    ):
        candidates: list[int] = []
        issue_latest = gh.latest_comment_id(issue)
        if isinstance(issue_latest, int):
            candidates.append(issue_latest)
        pr_conv = list(gh.pr_conversation_comments_after(pr, None))
        if pr_conv:
            candidates.append(max(comment.id for comment in pr_conv))
        state.set(_PR_LAST_COMMENT_ID, max(candidates) if candidates else 0)
        seeded = True

    if _seed_missing_watermark(
        state, "pr_last_review_comment_id",
        lambda: gh.pr_inline_comments_after(pr, None),
    ):
        seeded = True
    if _seed_missing_watermark(
        state, "pr_last_review_summary_id",
        lambda: gh.pr_reviews_after(pr, None),
    ):
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
    _record_pending_fix_bookmarks(
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

    issue_wm = _issue_side_watermark(ctx.state)
    orchestrator_ids = _wf._orchestrator_ids(ctx.state)
    return _drop_orchestrator_comments(
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
    wt = _drift_worktree(ctx)
    before_sha = _wf._head_sha(wt)
    wt, dev_result, paused = _wf._resume_dev_with_text(
        ctx.gh, ctx.spec, ctx.issue, ctx.state,
        _build_drift_resume_prompt(ctx.issue, filter_trusted(unread_pr_conv)),
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
    _bump_in_review_watermarks(ctx, issue_space_new=unread_pr_conv)
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
    if _wf._ignore_if_interrupted(ctx.issue, resume.dev_result):
        return True
    if resume.paused:
        return True
    _dispose_drift_result(ctx, unread_pr_conv, resume)
    return True


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
    if _final_docs_handoff_completed_for_head(ctx.state, head_sha):
        return True
    return ctx.gh.pr_is_approved(ctx.pr, head_sha=head_sha)


def _handle_mergeable_gate(ctx: _InReviewContext) -> None:
    """Manual-merge-only mergeability gate. An unmergeable PR parks awaiting
    human regardless of approval state -- the orchestrator never routes from
    here to `resolving_conflict` and never calls `gh.merge_pr`. A mergeable PR
    earns a one-shot HITL ping per head SHA when either the agent-approved
    final-docs handoff covers that head OR GitHub carries a real APPROVED
    review on that head, and no standing CHANGES_REQUESTED veto exists. A
    `quick_run`-labeled issue is exempt from the approval markers, so a
    mergeable quick-run PR with no standing CHANGES_REQUESTED earns the ping
    directly.
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
        _bump_in_review_watermarks(ctx)
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)
        return
    # mergeable: humans drive the merge. The ping advertises the PR as "ready
    # for review/merge", so it must only fire for a head the orchestrator has
    # reviewer-approved and documented (or one a human/bot formally approved in
    # GitHub, or a quick_run head exempt from those markers) AND carrying no
    # standing human veto; otherwise we would invite a manual merge over a stale
    # or rejected commit.
    head_sha = pr.head.sha
    if ctx.gh.pr_has_changes_requested(pr, head_sha=head_sha):
        return
    # `quick_run` is an explicit exemption from the approval markers: a clean
    # quick-run PR should still earn the ready ping without a final-docs marker
    # or an orchestrator APPROVED review, so bypass the approval gate for it --
    # the mergeable-head and no-CHANGES_REQUESTED guards above still apply.
    if not issue_has_label(ctx.issue, QUICK_RUN_LABEL) and not _head_is_approved(
        ctx, head_sha,
    ):
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
    _seed_legacy_in_review_watermarks(ctx.gh, ctx.issue, ctx.pr, ctx.state)
    issue_space_new, review_space_new, review_summary_new = (
        _scan_fresh_pr_feedback(ctx)
    )
    new_comments = issue_space_new + review_space_new + review_summary_new
    if _stay_parked(ctx.state, new_comments):
        return True
    if not new_comments:
        return False
    _route_feedback_to_fixing(
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
    HITL ping per head SHA so the human knows the PR is ready; a
    `quick_run` issue skips the approval markers entirely and earns the
    ping on any mergeable head with no standing CHANGES_REQUESTED. An
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
        _park_missing_pr_number(gh, issue, state)
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

    if _consume_fresh_feedback(ctx):
        return

    if _handle_user_content_drift(ctx):
        return

    _handle_mergeable_gate(ctx)
