# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fixing stage handler.

`_handle_fixing` owns the PR-feedback quiet window and the dev-resume /
push / hand-back-to-`validating` cycle. Two routes set the `fixing`
label:

  * `_handle_in_review` flips it the moment fresh PR feedback
    (issue-thread, PR-conversation, inline-review, or review-summary)
    is detected; the in_review handler deliberately leaves the in_review
    watermarks behind so this handler can read the triggering comments
    for its dev-resume prompt. This route records `pending_fix_at` +
    per-namespace `pending_fix_*_max_id` bookmarks plus the full
    `pending_fix_*_ids` batch lists, so `_reconstruct_pending_fix_batch`
    can rebuild the exact triggering batch even after the watermarks
    advance past it.
  * `_handle_validating` flips it BEFORE spawning the dev on a
    `CHANGES_REQUESTED` verdict so the dev-fix subphase is observably
    labeled `fixing` (the active job is "fixing reviewer-requested
    changes", not "validating"). This route does NOT set
    `pending_fix_at`; instead it records `pending_fix_reviewer_comment_id`
    (the id of the reviewer-feedback PR comment) as the lone replay anchor
    for `_reconstruct_pending_fix_batch`. The dev runs inline in the same
    tick and the validating handler flips back to `validating` itself on a
    pushed fix with `review_round` bumped (clearing the anchor). Only the
    parked outcomes (timeout / no-commit / dirty / push-fail) leave the
    fixing handler to own the awaiting-human cycle here.

Each tick the handler rescans unread feedback from the existing watermarks
(NOT the `pending_fix_*` bookmarks recorded by the route -- those remain
in pinned state as forensic hints and as the reconstruction source for
`_reconstruct_pending_fix_batch`). Newer comments arriving while
already labeled `fixing` are picked up by the same rescan and naturally
extend the debounce window because the freshest comment's timestamp
controls the wait. Once `IN_REVIEW_DEBOUNCE_SECONDS` has elapsed with no
newer comment, the handler builds a `_build_pr_comment_followup` prompt
over ALL unread surfaces and resumes the locked dev session via
`_resume_dev_with_text`.

On a pushed fix the handler advances `pr_last_comment_id`,
`pr_last_review_comment_id`, and `pr_last_review_summary_id` past the
just-consumed feedback (mirrors the legacy in_review fix path), clears
the bookmarks, updates `review_round` based on the route discriminator
`pending_fix_at` (set → reset to 0 for the in_review route whose
previous reviewer round was APPROVED; unset → bump by 1 for the
validating route whose previous round was CHANGES_REQUESTED so the
review cycle continues), and flips the label DIRECTLY back to
`validating` so the reviewer agent re-evaluates the freshened diff
next tick. Docs do not run on the pushed-fix exit -- the single docs
pass is deferred to the final-docs handoff after reviewer approval, so
running the docs stage against an unapproved diff here would just push
a no-op and waste a tick. On a failed resume (timeout, dirty worktree, push
failure, no-commit question) the disposition helpers from
`stages.validating` (`_handle_dev_fix_result`) handle the park; the
watermarks STILL advance past the feedback the dev did see, otherwise
the next tick would replay the original triggering comment indefinitely
and the awaiting-human gate could never unstick on a fresh human reply.

The no-new-feedback bounce (rescan finds nothing past the watermarks
even though the bookmarks recorded triggering ids) also relabels to
`validating` directly: there is no fix work to do, so the reviewer
re-evaluates the existing head.

A validating-route transient park (`push_failed` / `agent_timeout` /
`reviewer_timeout` / `reviewer_failed`) whose own recovery returns
`"stuck"` can still be unstuck when the underlying condition is
worktree drift: the per-tick base sync stands down on every
`awaiting_human` park, so a base advance that landed between the prior
push and this tick leaves the integration work nobody else will do
stranded. `_reconcile_parked_fixing` breaks that dead-lock by handing
the issue to `resolving_conflict` (which owns rebasing AND publishing a
PR branch) when the clean worktree is either BEHIND `<remote>/<base>`
(needs a rebase) or already rebased onto base but diverged from a stale
remote PR head (a rebase a prior run never pushed -- `_handle_resolving_conflict`
recognizes the already-rebased worktree and force-publishes it). The
drift route is deliberately gated on the validating-route stuck-transient
branch: parks shaped like a real agent question or a dirty worktree
(`park_reason=None`, `agent_silent`, `dirty_worktree`) stay parked even
when the worktree has drifted, because we cannot distinguish a genuine
"agent needs input" from a "nothing to fix" remark by inspection --
auto-recovering either would silently bypass the HITL contract. The
helper no-ops when the worktree is missing / dirty, or the worktree is
already in sync with the PR head.

Separately, an in_review-route resume that produces no commit but ends
with an explicit `ACK: <reason>` marker returns straight to `in_review`
without parking. Unmarked no-commit replies park awaiting human: we
cannot distinguish "agent has a real question" from "agent reported
nothing to change" by inspection, and auto-recovering either would
silently bypass the HITL contract. One exception, on both routes: when
the clean worktree HEAD is strictly ahead of the fetched remote PR
branch -- a fix a prior parked run committed but never published --
`_handle_dev_fix_result` pushes that stranded HEAD through its normal
publish tail and treats the run as a pushed fix instead of parking
(see `validating._stranded_fix_unpushed`). The stranded check outranks
the ACK fast path: an in_review-route ACK on that shape falls through
to the publish tail instead of relabeling, because the `in_review`
return would clear the bookmarks, advance the watermarks, and present
a PR head that is still missing the committed fix.

A park on a session-limit / session-failure reason (`agent_silent` /
`agent_timeout`) can be retried by an operator with an exact-line
`/orchestrator continue` comment (`_handle_continue_command`; a comment
that carries the command line AND real guidance still counts as the
command). Rather than resuming the dev on the command text -- which would
drop the review feedback the poisoned session never addressed -- the
handler rechecks `park_reason`, drops the poisoned dev session so the retry
re-grounds a fresh one on the committed branch, and replays the PRESERVED
feedback batch (`_reconstruct_pending_fix_batch`): the `pending_fix_*`
bookmarks on the in_review route, or the single
`pending_fix_reviewer_comment_id` PR-comment anchor on the validating route
(the reviewer's CHANGES_REQUESTED feedback that
`_handle_validating_changes_requested` posted before the dev parked). ALL
fresh feedback (the command comment and any guidance posted with or beside
it) is carried verbatim so nothing the operator wrote is dropped. The
command is handled on BOTH routes; when NEITHER route has a reconstructable
batch (a validating-route park whose anchor was never recorded or has since
been deleted) a content-free continue is refused while a continue that came
WITH genuine guidance falls through to the normal resume so that guidance
drives the dev. Parks that still need real human guidance -- a genuine
agent question or a dirty worktree (both `park_reason=None`), or any
eligible park with no reconstructable batch -- refuse a content-free
continue: the command is consumed and a note is posted, and the issue
stays parked.

PR-state terminals (merged / closed-without-merge / open-PR-with-closed-issue)
mirror the in_review arcs so an external manual merge or rejection while
the issue is mid-fix still finalizes to `done` / `rejected` with branch
cleanup. Closed `fixing` issues are surfaced by the closed-issue sweep
specifically for this contract.

Open `fixing` issues touch only their own pinned state and worktree, so
the label is deliberately NOT listed in `workflow._FAMILY_AWARE_LABELS`
and `tick()` routes it through the fan-out bucket.

ALL workflow-owned helpers (`_park_awaiting_human`, `_resume_dev_with_text`,
`_handle_dev_fix_result`, `_comment_created_at`, `_now_iso`, the
worktree plumbing, the messaging helpers re-exported into `workflow`)
are reached through the parent module via `from .. import workflow as _wf`
at call time. Tests rely on `patch.object(workflow, "_foo", ...)`
intercepting calls made from inside the stage handler, so the handler
must NOT direct-import these names from `workflow_messages` / `worktrees`
/ sibling stage modules; doing so would bind a stable reference that the
patch could not affect.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.comment_trust import filter_trusted
from orchestrator.config import RepoSpec
from orchestrator.state_machine import WorkflowLabel
from orchestrator.github import GitHubClient, PinnedState


# Pinned-state keys this stage reads and writes.
_AWAITING_HUMAN = "awaiting_human"
_PENDING_FIX_AT = "pending_fix_at"
_PARK_REASON = "park_reason"
_REVIEW_ROUND = "review_round"
_CONFLICT_ROUND = "conflict_round"


@dataclass(frozen=True)
class _FixingFeedback:
    issue_space: list
    review_comments: list
    review_summaries: list
    all_items: list


@dataclass(frozen=True)
class _ParkedFixingDecision:
    stop: bool
    replay_batch: Optional[list] = None


@dataclass(frozen=True)
class _FixingContext:
    """The per-tick `fixing` invocation handles, bundled so the parked-dispatch,
    validating-recovery, continue-command, resume, and reconcile helpers thread
    them as a single value instead of five positional arguments (mirrors
    validating's `_RequestedChanges`). `pr` is the live PR `_fixing_preflight`
    fetched this tick; not every consumer reads it.
    """
    gh: GitHubClient
    spec: RepoSpec
    issue: Issue
    state: PinnedState
    pr: Any


@dataclass(frozen=True)
class _FixingResumeRun:
    """The outcome of one locked dev resume: the worktree it ran in, the agent
    result, whether an operator paused mid-run, and the HEAD before/after.
    """
    worktree: Path
    dev_result: AgentResult
    paused: bool
    before_sha: Optional[str]
    after_sha: Optional[str]


def _fixing_preflight(gh: GitHubClient, spec: RepoSpec, issue: Issue, state):
    """Fetch the PR and run the pre-rescan guards shared with
    `_handle_in_review`: PR-state terminals, a closed issue with no
    resolvable PR, and a `fixing` label with no pinned `pr_number`.

    Returns the fetched PR to continue the fix loop on, or ``None`` when
    the tick is fully handled -- a terminal finalized, a closed issue was
    left alone, a missing-PR park was posted, or the PR fetch failed -- and
    the caller must return immediately.
    """
    from orchestrator import workflow as _wf

    pr_number = state.get("pr_number")
    # Bind `pr` up front so the post-terminal guard below can branch on
    # it even when `pr_number` is None (in which case the fetch is
    # skipped entirely).
    pr = None

    # PR-state terminals (mirrors `_handle_in_review`). Run BEFORE any
    # rescan / debounce so a closed-fixing issue with a merged PR
    # finalizes to `done` on this tick instead of sitting closed +
    # `fixing` forever, and an external merge on an open issue also
    # short-circuits the resume cycle.
    #
    # PyGithub failures here are typically transient (network blip, rate
    # limit, 5xx). Catch and bail with `pr=None` so the caller also
    # short-circuits -- the next tick re-fetches and picks up wherever we
    # left off; the watermarks are unchanged so no feedback is lost.
    if pr_number is not None:
        try:
            pr = gh.get_pr(int(pr_number))
        except Exception:
            _wf.log.exception(
                "issue=#%s could not fetch PR #%s in fixing terminal "
                "branch; falling through", issue.number, pr_number,
            )
            pr = None
        if _wf._drain_review_pr_terminals(
            gh, spec, issue, state, pr, stage="fixing",
        ):
            return None

    # Closed issue with no PR (or a PR lookup failure): nothing to
    # finalize via the PR-state arcs above. Leave alone rather than
    # parking a closed issue.
    if getattr(issue, "state", "open") == "closed":
        _wf.log.info(
            "repo=%s issue=#%s closed fixing issue with no resolvable PR; "
            "leaving alone (relabel manually to finalize)",
            spec.slug, issue.number,
        )
        return None

    if pr_number is None:
        # `fixing` is only ever entered with a recorded PR (in_review
        # holds the PR before routing). Reaching here means a manual
        # relabel from outside that route -- park once and surface to a
        # human; the dev-resume path needs the PR to push a fix.
        if state.get(_AWAITING_HUMAN):
            return None
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `fixing` without a pinned "
            "`pr_number`; manual relabeling suspected. Set the workflow "
            "label back to `in_review` (or `validating`) after attaching "
            "a PR.",
            reason="missing_pr_number",
        )
        gh.write_pinned_state(issue, state)
        return None

    # `pr_number` was set but `gh.get_pr` raised above. The exception is
    # already logged; bail this tick so the caller's rescan does not
    # dereference `None`. PyGithub failures here are typically transient
    # (network blip, rate limit, 5xx), so the next tick re-fetches and
    # picks up wherever we left off; the watermarks are unchanged so no
    # feedback is lost.
    if pr is None:
        return None

    return pr


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
    issue_space = _new_issue_space_feedback(gh, issue, pr, state)
    review_comments = _new_review_comment_feedback(gh, pr, state)
    review_summaries = _new_review_summary_feedback(gh, pr, state)
    return _FixingFeedback(
        issue_space=issue_space,
        review_comments=review_comments,
        review_summaries=review_summaries,
        all_items=issue_space + review_comments + review_summaries,
    )


def _dispatch_continue_command(
    ctx: _FixingContext, feedback: _FixingFeedback,
) -> Optional[_ParkedFixingDecision]:
    """Apply a `/orchestrator continue` command to a parked tick.

    Returns a `_ParkedFixingDecision` when the command was resolved (a refused
    content-free continue -> `stop=True`; an accepted replay -> `stop=False`
    with the preserved batch), or ``None`` for the "passthrough" case (the
    command arrived WITH genuine guidance on a park with no replayable batch),
    where the caller falls through to the validating-recovery / normal-resume
    path so that guidance drives the dev.
    """
    action, replay_items = _handle_continue_command(ctx, feedback)
    if action == "refuse":
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)
        return _ParkedFixingDecision(stop=True)
    if action == "replay":
        return _ParkedFixingDecision(stop=False, replay_batch=replay_items)
    return None


def _dispatch_validating_recovery(
    ctx: _FixingContext, feedback: _FixingFeedback, park_reason,
) -> Optional[_ParkedFixingDecision]:
    """Attempt silent recovery of a validating-route transient park.

    Returns a stop-decision when this branch owns the tick (a stuck transient
    rerouted to `resolving_conflict` on drift, or a resolved transient flipped
    back to `validating`), or ``None`` to fall through to the stay-parked /
    clear-park default.

    Only fires when the park reason can resolve without a human comment AND the
    issue arrived via the validating route (CHANGES_REQUESTED dev fix). The
    `_handle_validating` CHANGES_REQUESTED branch flips to `fixing` BEFORE
    spawning the dev, so a transient park (`push_failed` / `agent_timeout`)
    lands under `fixing` instead of `validating`; without this branch the issue
    would sit forever awaiting a human comment the underlying condition does
    not produce. Recovery must NOT run on the in_review route: that route
    advances the PR-feedback watermarks past the human comment even on a
    timed-out resume, and the shared helper bumps `review_round` on its
    `pushed` outcome, which the in_review route resets to 0 -- so a deferred
    push there would consume feedback without a fix and mis-account the round.
    The route discriminator is `pending_fix_at` (set by the in_review route,
    unset by the validating route).
    """
    from orchestrator import workflow as _wf

    validating_routed = ctx.state.get(_PENDING_FIX_AT) is None
    if (
        feedback.all_items
        or park_reason not in _wf._VALIDATING_TRANSIENT_PARK_REASONS
        or not validating_routed
    ):
        return None

    recovery = _wf._try_recover_validating_transient_park(
        ctx.spec, ctx.issue, ctx.state,
    )
    if recovery == "stuck":
        # The transient condition has not resolved on its own (e.g.
        # `push_failed` keeps failing). When the worktree has drifted from
        # the PR head in the meantime, hand the reconciliation to
        # `resolving_conflict` rather than sit parked forever -- the per-tick
        # base sync deliberately stands down on every `awaiting_human` park,
        # so nobody else will sync this worktree. Limiting the drift route to
        # this branch keeps the HITL contract intact: question / dirty /
        # silent / in_review-route transient parks fall through to the bare
        # stay-parked return below and keep waiting for a human comment.
        _reconcile_parked_fixing(ctx)
        return _ParkedFixingDecision(stop=True)

    # Conditions resolved (either no fix landed or a deferred push finished).
    # Clear the park flags and flip back to `validating` so the reviewer
    # re-evaluates the current head next tick. The helper has already bumped
    # `review_round` when a fix landed (push_failed, or agent_timeout that
    # finished its push). Clear the pending_fix_* bookmarks defensively: this
    # branch ONLY fires when `pending_fix_at` was already None, so the clear is
    # a no-op in normal flow, but a stale bookmark from an earlier route would
    # otherwise mis-flag the next reviewer round.
    ctx.state.set(_AWAITING_HUMAN, False)
    ctx.state.set(_PARK_REASON, None)
    _clear_pending_fix_bookmarks(ctx.state)
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)
    return _ParkedFixingDecision(stop=True)


def _dispatch_parked_fixing(
    ctx: _FixingContext, feedback: _FixingFeedback,
) -> _ParkedFixingDecision:
    """Reconcile a `fixing` tick that arrived with `awaiting_human` set.

    Returns a decision object. ``stop=True`` means the tick is fully handled
    and the caller must return immediately (auto-rebase park, a refused
    `/orchestrator continue`, a silent validating-route recovery, a
    worktree-drift reroute, or a stay-parked-until-fresh-reply). ``stop=False``
    clears the park and the caller proceeds to the resume; `replay_batch` is
    the preserved feedback batch when an accepted `/orchestrator continue`
    replays it, otherwise ``None``.
    """
    from orchestrator import workflow as _wf

    park_reason = ctx.state.get(_PARK_REASON)
    # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to the
    # `_sync_pr_worktree_to_base` retry loop -- the operator's new comment is
    # the "retry the rebase" signal, NOT fresh PR feedback for the dev
    # fix-loop. Stay silent so the refresh keeps ownership of the comment;
    # resuming the dev here would spawn it on a prompt that has nothing to do
    # with the outstanding fix.
    if park_reason in _wf._AUTO_REBASE_PARK_REASONS:
        return _ParkedFixingDecision(stop=True)

    # `/orchestrator continue` operator command (exact line, so a comment
    # carrying the command AND real guidance still counts). Handled on BOTH
    # routes so a session-failure park (`agent_silent` / `agent_timeout`) never
    # resumes the dev on the bare command text. A "replay" or "refuse"
    # decision owns the tick; a "passthrough" returns None and falls through.
    if _wf._parse_orchestrator_continue(feedback.issue_space):
        decision = _dispatch_continue_command(ctx, feedback)
        if decision is not None:
            return decision

    recovery = _dispatch_validating_recovery(ctx, feedback, park_reason)
    if recovery is not None:
        return recovery

    if not feedback.all_items:
        # All other awaiting_human shapes (question parks, dirty worktree
        # parks, silent-crash parks, in_review-route transients) stay parked
        # until a fresh human reply lands. We cannot distinguish "agent has a
        # real question" from "agent reported nothing to change" by inspection
        # -- both surface through `_on_question` with `park_reason=None` -- so
        # auto-routing either would silently bypass the HITL contract. The same
        # applies to a clean in-sync worktree on the in_review route: the dev
        # may have replied with a real question that needs a human to resolve,
        # so the only automatic exit from `fixing` for the in_review route is
        # the ACK fast path in the resume tail (on the same tick the dev
        # explicitly marks its no-commit reply with `ACK:`).
        return _ParkedFixingDecision(stop=True)

    ctx.state.set(_AWAITING_HUMAN, False)
    ctx.state.set(_PARK_REASON, None)
    return _ParkedFixingDecision(stop=False)


def _fixing_debounce_open(
    feedback: _FixingFeedback, replay_batch,
) -> bool:
    """True while the quiet window is still open: hold the resume until no
    comment has landed for `IN_REVIEW_DEBOUNCE_SECONDS`.

    A newer comment arriving on a later tick is naturally picked up by the
    rescan, which extends the wait because the freshest timestamp controls
    the gate. Comments without a usable timestamp (older fakes, PyGithub
    edge cases) do not block the resume; in production `created_at` /
    `submitted_at` are always set. An accepted `/orchestrator continue`
    (`replay_batch` set) skips the wait entirely -- it is a deliberate
    operator signal, not chatter to debounce.
    """
    from orchestrator import workflow as _wf

    if replay_batch is not None:
        return False
    now = datetime.now(timezone.utc)
    latest_ts: Optional[datetime] = None
    for feedback_item in feedback.all_items:
        ts = _wf._comment_created_at(feedback_item)
        if ts is None:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
    return (
        latest_ts is not None
        and (now - latest_ts).total_seconds() < config.IN_REVIEW_DEBOUNCE_SECONDS
    )


def _apply_fix_review_round(state, pending_fix_at_was_set: bool) -> None:
    """Update `review_round` on a pushed fix per the route discriminator.

      * in_review->fixing (`pending_fix_at` was set): reset to 0. The
        previous reviewer round was APPROVED (the in_review HITL ping is
        gated on approval); the new fix starts a fresh round-count so
        MAX_REVIEW_ROUNDS does not trip prematurely on issues that pass
        back through review after a human PR comment.
      * validating->fixing (a CHANGES_REQUESTED dev fix that parked and
        was finished via a human reply): bump. The previous round was
        CHANGES_REQUESTED, not APPROVED, so we are still in the same
        review cycle and the round counter must advance to keep
        MAX_REVIEW_ROUNDS accounting honest.
    """
    if pending_fix_at_was_set:
        state.set(_REVIEW_ROUND, 0)
    else:
        round_n = int(state.get(_REVIEW_ROUND) or 0)
        state.set(_REVIEW_ROUND, round_n + 1)


def _run_fixing_resume(
    ctx: _FixingContext, followup: str,
) -> _FixingResumeRun:
    """Ensure the worktree, resume the locked dev session over `followup`,
    refresh the user-content drift hash, and read HEAD before/after.

    The hash refresh includes any human issue-thread comments we just fed to
    the dev via `followup`. Without it, the next tick that runs
    `_handle_validating` (or any other handler that calls
    `_detect_user_content_change`) would see those consumed comments as fresh
    user-content drift and resume the dev a second time on input it has already
    handled. Mirrors the hash refresh `_handle_in_review` does at the moment it
    routes to `fixing`. Refresh on BOTH success and failure paths: the dev saw
    the comments via the prompt either way, so the baseline must move with the
    consumption regardless of whether the agent pushed a fix this tick.

    HEAD is read only when the run did not time out -- the timeout branch of
    `_handle_dev_fix_result` returns before it would use `after_sha`, and
    reading here would burn an extra `_head_sha` the timeout path never did.
    """
    from orchestrator import workflow as _wf

    wt = _wf._worktree_path(ctx.spec, ctx.issue.number)
    if not wt.exists():
        wt = _wf._ensure_worktree(
            ctx.spec, ctx.issue.number,
            branch=_wf._resolve_branch_name(ctx.state, ctx.spec, ctx.issue.number),
        )
    before_sha = _wf._head_sha(wt)
    wt, dev_result, paused = _wf._resume_dev_with_text(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, followup, pause_guard=True,
    )
    ctx.state.set("last_agent_action_at", _wf._now_iso())
    ctx.state.set(
        "user_content_hash",
        _wf._compute_user_content_hash(
            ctx.issue, _wf._orchestrator_ids(ctx.state),
        ),
    )
    after_sha = None if dev_result.timed_out else _wf._head_sha(wt)
    return _FixingResumeRun(
        worktree=wt,
        dev_result=dev_result,
        paused=paused,
        before_sha=before_sha,
        after_sha=after_sha,
    )


def _fixing_ack_fast_path(
    ctx: _FixingContext,
    wt: Path,
    feedback: _FixingFeedback,
    dev_result: AgentResult,
    after_sha: Optional[str],
) -> bool:
    """In_review-route ACK fast path. Returns True (and relabels to
    `in_review`) when the dev's no-commit reply carried an explicit
    `ACK: <reason>` marker vouching that the PR feedback needs no actionable
    change; False to fall through to `_handle_dev_fix_result`.

    A vague "continue" / "ok" nudge should not strand a complete, mergeable PR
    in `fixing`, so an ack returns to `in_review` (re-arming the ready-ping)
    instead of parking.

    The fast path stands down on the stranded-fix shape: the ack vouches for
    the *feedback*, not for the publish state, so when the clean HEAD is
    strictly ahead of the remote PR branch (a fix a prior parked run committed
    but never pushed -- e.g. a dirty-park whose stray files were later cleaned
    up) relabeling to `in_review` here would clear the bookmarks, advance the
    watermarks, and present a PR head that is still missing the committed fix.
    Falling through lets `_handle_dev_fix_result` publish the stranded HEAD
    through its normal push tail and the pushed-fix exit route the freshened
    head back to the reviewer. The stranded check is skipped when `after_sha`
    is unreadable (mirrors `_handle_dev_fix_result`'s own gate -- no pushing
    blind off a worktree whose HEAD we could not read).
    """
    from orchestrator import workflow as _wf

    ack_reason = _wf._drift_ack_reason(dev_result.last_message or "")
    if not ack_reason or (
        after_sha and _wf._stranded_fix_unpushed(ctx.spec, wt, ctx.state, ctx.issue)
    ):
        return False
    _advance_consumed_watermarks(ctx.state, feedback)
    _clear_pending_fix_bookmarks(ctx.state)
    quoted = "> " + ack_reason.replace("\n", "\n> ")
    _wf._post_issue_comment(
        ctx.gh, ctx.issue, ctx.state,
        ":speech_balloon: dev session reports the PR feedback needs "
        f"no change:\n\n{quoted}\n\nReturning to `in_review`.",
    )
    # The session is alive and producing a coherent ack, so reset the
    # silent-park streak (mirrors the drift-ack handling).
    ctx.state.set("silent_park_count", 0)
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.IN_REVIEW)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)
    return True


def _resume_fixing_and_dispatch_result(
    ctx: _FixingContext,
    feedback: _FixingFeedback,
    replay_batch,
) -> None:
    """Resume the locked dev session over the unread feedback (or a preserved
    `/orchestrator continue` batch), then dispatch the result: the in_review-
    route ACK fast path, the pushed-fix bounce back to `validating`, or a park
    via `_handle_dev_fix_result`.

    Runs after the quiet window has elapsed. Owns the resume, the interrupted /
    live-paused guards, the consumed-watermark advance, and the route round
    bookkeeping.
    """
    from orchestrator import workflow as _wf

    # Capture the route discriminator BEFORE the bookmark-clear branches below.
    # `pending_fix_at` is untouched between the tick's capture point and here
    # (no reachable path clears it in between), and the pushed-fix tail clears
    # the bookmarks only after this read.
    pending_fix_at_was_set = ctx.state.get(_PENDING_FIX_AT) is not None

    # On an accepted `/orchestrator continue`, resume on the PRESERVED batch
    # (plus any new feedback that came with the command), not the command
    # text -- the whole point of the command is to not lose the review
    # feedback the parked session never addressed.
    followup = _wf._build_pr_comment_followup(
        replay_batch if replay_batch is not None else feedback.all_items
    )
    run = _run_fixing_resume(ctx, followup)

    # A shutdown-killed (interrupted) resume is ignored entirely: its partial
    # last_message is not a real ACK or question, and `_handle_dev_fix_result`
    # refuses to publish an interrupted run regardless of HEAD. Bail WITHOUT
    # persisting state -- the ACK fast path, the consumed-watermark advance,
    # and the write below never run, and the awaiting_human reset / hash
    # refresh staged earlier this tick are dropped because we skip
    # `write_pinned_state`. The next tick re-discovers the same comments
    # (watermarks unmoved, bookmarks intact, awaiting_human unchanged) and
    # re-feeds them to a fresh dev session. This MUST cover the new-commit
    # case too: a kill that had advanced HEAD would otherwise fall through to
    # `_handle_dev_fix_result` (returns False, no push) and the watermark
    # advance below would consume the feedback while the local commit sits
    # unpushed -- the next tick would then see no feedback and bounce a PR
    # head that is missing the fix. Leaving the commit on disk lets a later
    # clean run republish it via the stranded-fix tail.
    if run.dev_result.interrupted:
        return

    # Live pause applied while the agent ran: an operator added `paused` (or
    # `backlog`) mid-run. Honor the decision `_resume_dev_with_text` already
    # made (propagated, not re-fetched) and stop before the ACK fast path, the
    # stranded-fix publish, `_handle_dev_fix_result`, the watermark advance, or
    # any relabel / pinned-state write. The committed work stays on the branch,
    # so once the label is removed the normal recovered / stranded-fix path
    # republishes it.
    if run.paused:
        return

    # ACK fast path (in_review route only): the dev made no commit but
    # explicitly signaled via the `ACK: <reason>` marker that the PR feedback
    # carries no actionable change. The validating CHANGES_REQUESTED route
    # (`pending_fix_at` unset) is excluded -- the reviewer DID request a
    # concrete change, so an ACK there falls through to `_handle_dev_fix_result`,
    # which parks for the human unless its stranded-fix check publishes a
    # committed-but-unpushed fix instead (`validating._stranded_fix_unpushed`).
    if (
        pending_fix_at_was_set
        and not run.dev_result.timed_out
        and (not run.after_sha or run.after_sha == run.before_sha)
        and _fixing_ack_fast_path(
            ctx, run.worktree, feedback, run.dev_result, run.after_sha,
        )
    ):
        return

    pushed = _wf._handle_dev_fix_result(
        ctx.gh, ctx.spec, ctx.issue, ctx.state, run.worktree, run.dev_result,
        run.before_sha, after_sha=run.after_sha,
    )

    # Advance the three in_review watermarks ONLY to the max id actually fed to
    # the dev on each surface (ratcheted against the current watermark).
    # Deliberately tighter than `_bump_in_review_watermarks`, which also pulls
    # in `gh.latest_comment_id(issue)`: a human issue-thread comment that
    # landed AFTER `feedback` was built but BEFORE this write was never quoted
    # in the dev's `_build_pr_comment_followup` prompt, so silently moving the
    # watermark past it would swallow real feedback.
    #
    # This applies to BOTH paths:
    #
    #   * On a pushed fix, the next in_review tick (after `validating`
    #     completes) must rediscover the concurrent comment as fresh PR
    #     feedback.
    #
    #   * On park/failure (timeout / dirty / push fail / no-commit), the next
    #     fixing tick must also rediscover it -- otherwise the
    #     `awaiting_human and not new_feedback` gate fires and the concurrent
    #     human comment is silently dropped, breaking the "comments arriving
    #     while already labeled `fixing`" contract on every failure mode.
    #
    # The orchestrator's own park comment posted by `_park_awaiting_human`
    # (issue id-space, body carries `_ORCH_COMMENT_MARKER` and its id is
    # recorded in `orchestrator_comment_ids`) does NOT need a watermark bump to
    # avoid replay: the next tick's rescan filters by both id and body marker,
    # so the park comment is dropped even when the watermark sits below it.
    _advance_consumed_watermarks(ctx.state, feedback)

    if not pushed:
        ctx.gh.write_pinned_state(ctx.issue, ctx.state)
        return

    # Bookmarks served their purpose; clear them so a later in_review->fixing
    # route writes fresh values rather than mixing rounds.
    # `_apply_fix_review_round` then updates `review_round` per the route
    # discriminator (`pending_fix_at_was_set`), and we flip DIRECTLY to
    # `validating` so the reviewer re-evaluates the new head next tick. Docs do
    # not run on this exit -- the single docs pass is deferred to the final-docs
    # handoff after reviewer approval, so running the docs stage against an
    # unapproved diff here would just push a no-op and waste a tick.
    _clear_pending_fix_bookmarks(ctx.state)
    _apply_fix_review_round(ctx.state, pending_fix_at_was_set)
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.VALIDATING)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _handle_fixing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)

    pr = _fixing_preflight(gh, spec, issue, state)
    if pr is None:
        return

    feedback = _rescan_fixing_feedback(gh, issue, pr, state)

    # `replay_batch` is set only by an accepted `/orchestrator continue`
    # command inside `_dispatch_parked_fixing`: the PRESERVED PR-feedback batch
    # (plus any genuinely new feedback that arrived with the command) to resume
    # the fresh dev on, instead of the per-tick rescan. It skips the debounce
    # and re-grounds a dropped session in the resume tail.
    #
    # `_dispatch_parked_fixing` bails (`stop=True`) unless something new has
    # arrived since the park bump: the watermarks were advanced past the
    # previously-consumed feedback, so `feedback` can only carry genuinely new
    # content, and without that guard a single poisoned tick would loop on
    # every poll, spamming the same dev-resume prompt.
    replay_batch: Optional[list] = None
    if state.get(_AWAITING_HUMAN):
        parked = _dispatch_parked_fixing(
            _FixingContext(gh, spec, issue, state, pr), feedback,
        )
        if parked.stop:
            return
        replay_batch = parked.replay_batch

    # Watermarks already cover the triggering bookmarks (a prior tick consumed
    # them, or an operator advanced them manually). Nothing left to address;
    # clear the route bookkeeping and bounce back to `validating` so the
    # reviewer re-evaluates against the current head instead of leaving the
    # issue stuck in `fixing` with no work.
    if not feedback.all_items:
        _clear_pending_fix_bookmarks(state)
        gh.set_workflow_label(issue, WorkflowLabel.VALIDATING)
        gh.write_pinned_state(issue, state)
        return

    if _fixing_debounce_open(feedback, replay_batch):
        return

    _resume_fixing_and_dispatch_result(
        _FixingContext(gh, spec, issue, state, pr), feedback, replay_batch,
    )


def _fixing_drift_reason(
    ctx: _FixingContext, wt: Path, base_ref: str,
) -> Optional[str]:
    """Classify how a clean parked `fixing` worktree has drifted from its PR,
    or return ``None`` when it is in sync (the transient park is the real
    blocker, not drift).

    Two drift shapes both reconcile via `resolving_conflict`:

      * worktree BEHIND `<remote>/<base>` -> needs a rebase.
      * worktree already rebased locally but the rewrite was never pushed, so
        local HEAD differs from the (stale) remote PR head -> needs a
        force-publish (`_handle_resolving_conflict` recognizes an already-
        rebased worktree and publishes it instead of parking).

    Trusts the once-per-tick base fetch `_refresh_base_and_worktrees` ran
    before dispatch (mirrors `_sync_worktree_with_base`, which also measures
    behind without re-fetching). A stale ref can only undercount (stay parked)
    or, on the rare case the per-tick fetch failed, overcount -- and
    `_handle_resolving_conflict` re-fetches before it acts, so an overcount
    self-corrects. The routing decision is cheap: base drift is a local
    `rev-list HEAD..<remote>/<base>`, and the unpushed-rebase check compares
    local HEAD to `pr.head.sha` (the live remote head fetched this tick).
    """
    from orchestrator import workflow as _wf

    behind_r = _wf._git("rev-list", "--count", f"HEAD..{base_ref}", cwd=wt)
    if behind_r.returncode != 0:
        return None
    try:
        behind = int((behind_r.stdout or "0").strip() or "0")
    except ValueError:
        return None

    if behind > 0:
        return f"{behind} commit(s) behind `{base_ref}`"

    # On top of base: is the local branch out of sync with the PR head? `pr`
    # was fetched fresh this tick, so `pr.head.sha` is the live remote head. A
    # mismatch means the worktree carries a rebase that was never pushed --
    # `_handle_resolving_conflict` republishes it (over a stale,
    # orchestrator-produced PR head).
    local_head = _wf._head_sha(wt) or ""
    pr_head = getattr(getattr(ctx.pr, "head", None), "sha", None) or ""
    if local_head and pr_head and local_head != pr_head:
        return (
            f"already rebased onto `{base_ref}`, but the PR head "
            f"(`{pr_head[:8]}`) is stale (local `{local_head[:8]}`)"
        )
    return None  # in sync with the PR -> genuine dev question


def _post_fixing_conflict_notice(
    ctx: _FixingContext, pr_number: int, drift_reason: str,
) -> None:
    """Post the worktree-drift reroute notice to the PR, swallowing a transient
    comment failure (the relabel still proceeds; the next tick re-fetches)."""
    from orchestrator import workflow as _wf

    try:
        _wf._post_pr_comment(
            ctx.gh, pr_number, ctx.state,
            f":mag: PR worktree is out of sync ({drift_reason}) and the `fixing` "
            "fix-loop is parked on a stuck transient condition that the "
            "self-recovery could not clear. Routing `fixing` -> "
            "`resolving_conflict` to reconcile the branch before the next "
            "reviewer round.",
        )
    except Exception:
        _wf.log.exception(
            "issue=#%s could not post worktree-drift reroute notice to PR #%s",
            ctx.issue.number, pr_number,
        )


def _route_parked_fixing_to_conflict(
    ctx: _FixingContext, drift_reason: str,
) -> None:
    """Relabel a drifted parked `fixing` worktree to `resolving_conflict` so
    its handler reconciles the branch before the next reviewer round.

    The `pending_fix_*` bookmarks and in_review watermarks are left untouched
    so the eventual `in_review` re-entry still re-discovers the feedback
    (mirrors the refresh-time conflict detour).
    """
    from orchestrator import workflow as _wf

    pr_number = int(ctx.state.get("pr_number"))
    # Seed `conflict_round` only when absent so a re-entry preserves the cap
    # counter (mirrors `_route_pr_worktree_to_resolving_conflict`).
    if ctx.state.get(_CONFLICT_ROUND) is None:
        ctx.state.set(_CONFLICT_ROUND, 0)
    ctx.state.set(_AWAITING_HUMAN, False)
    ctx.state.set(_PARK_REASON, None)
    _post_fixing_conflict_notice(ctx, pr_number, drift_reason)
    ctx.gh.emit_event(
        _CONFLICT_ROUND,
        issue_number=ctx.issue.number,
        stage="fixing",
        pr_number=pr_number,
        sha=getattr(getattr(ctx.pr, "head", None), "sha", None) or None,
        action="entered",
        conflict_round=int(ctx.state.get(_CONFLICT_ROUND) or 0),
        review_round=int(ctx.state.get(_REVIEW_ROUND) or 0),
        retry_count=ctx.state.get("retry_count"),
    )
    _wf.log.info(
        "issue=#%s parked `fixing` worktree is out of sync (%s); routing -> "
        "resolving_conflict",
        ctx.issue.number, drift_reason,
    )
    ctx.gh.set_workflow_label(ctx.issue, WorkflowLabel.RESOLVING_CONFLICT)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _reconcile_parked_fixing(ctx: _FixingContext) -> bool:
    """Hand a stuck validating-route transient `fixing` park to
    `resolving_conflict` on worktree drift.

    Called from the `recovery == "stuck"` branch of
    `_dispatch_validating_recovery`: `_try_recover_validating_transient_park`
    could not clear the transient condition (e.g. `push_failed` keeps
    failing), but the underlying cause may be a base advance that landed while
    the issue was parked. The per-tick base sync (`_sync_pr_worktree_to_base`)
    deliberately stands down on every `awaiting_human` park, so the integration
    work nobody else will do is stranded and the issue sits parked forever.

    Returns False (issue stays parked) when the worktree is missing, dirty (an
    operator may be inspecting a dirty-tree park), or the worktree is already
    in sync with the PR head (the transient condition is the real blocker, not
    drift). Returns True after routing the drift to `resolving_conflict`.
    """
    from orchestrator import workflow as _wf

    spec = ctx.spec
    wt = _wf._worktree_path(spec, ctx.issue.number)
    if not wt.exists():
        return False
    if _wf._worktree_dirty_files(wt):
        return False

    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    drift_reason = _fixing_drift_reason(ctx, wt, base_ref)
    if drift_reason is None:
        return False

    _route_parked_fixing_to_conflict(ctx, drift_reason)
    return True


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
    issue_ids = _pending_fix_id_set(
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
    review_ids = _pending_fix_id_set(
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
    summary_ids = _pending_fix_id_set(
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


def _reconstruct_pending_fix_batch(gh, issue, pr, state) -> list:
    """Rebuild the exact feedback batch that drove the `in_review` -> `fixing`
    route from the pinned `pending_fix_*` metadata.

    The per-tick rescan in `_handle_fixing` reads from the in_review
    watermarks, which advance past the triggering feedback the moment a dev
    resume consumes it -- so once a fix has been attempted the batch can no
    longer be recovered by rescanning. This helper reconstructs it from the
    persisted ids instead, returned in the same order the route built them --
    issue-space (issue-thread + PR-conversation) then inline review comments
    then review summaries, each sorted by id. Filtering by the recorded id set
    inherently drops the orchestrator's own comments (their ids were never in
    the batch) and survives watermark advancement because the fetch is
    unbounded. A batch item deleted on GitHub since the route simply drops out.

    The validating -> fixing route preserves no `pending_fix_*_ids`; its lone
    replay anchor is the reviewer-feedback PR comment recorded in
    `pending_fix_reviewer_comment_id`. `_reviewer_anchor_comment` re-fetches it
    and it is prepended to the batch OUTSIDE `filter_trusted` (it is the
    orchestrator's own trusted reviewer output, which the author allowlist
    would otherwise drop). Consulted ONLY on the validating route
    (`pending_fix_at` unset): a stale anchor left behind by an earlier
    validating park must not be prepended to an in_review-route batch. The two
    routes are mutually exclusive in practice, so the anchor is de-duplicated
    against the id-set batch defensively.

    Re-apply the author allowlist at reconstruction time, not only at route
    time: an issue parked before the trust gate shipped can carry untrusted ids
    in `pending_fix_*_ids`, and `ALLOWED_ISSUE_AUTHORS` may change between the
    route and the `/orchestrator continue` replay. Existing parked issues that
    carry only `pending_fix_*_max_id` (no id lists) get the conservative
    single-item reconstruction from `_pending_fix_id_set`.
    """
    trusted_batch = filter_trusted(
        _reconstruct_issue_space(gh, issue, pr, state)
        + _reconstruct_review_comments(gh, pr, state)
        + _reconstruct_review_summaries(gh, pr, state)
    )
    if state.get(_PENDING_FIX_AT) is None:
        anchor = _reviewer_anchor_comment(gh, pr, state)
        if anchor is not None and all(
            feedback_item.id != anchor.id for feedback_item in trusted_batch
        ):
            return [anchor] + trusted_batch
    return trusted_batch


def _advance_consumed_watermarks(
    state, feedback: _FixingFeedback,
) -> None:
    """Advance the three in_review watermarks ONLY to the max id consumed
    per surface, ratcheted against the existing watermark.

    Called once on every dev-result outcome (BOTH the pushed-fix path
    AND the park/failure path) before the pushed/non-pushed split, so
    a concurrent human comment that landed between `feedback` and
    this call survives to the next tick on either branch. The broader
    `_bump_in_review_watermarks` is deliberately NOT used here: it
    also pulls in `gh.latest_comment_id(issue)`, which could leap the
    watermark past a concurrent issue-thread comment the dev never saw
    in its prompt -- silently swallowing real feedback on the pushed
    path (the next in_review tick would miss it) and on the
    park/failure path (the next fixing tick's
    `awaiting_human and not new_feedback` gate would drop it).
    """
    cur_issue_wm = state.get("pr_last_comment_id")
    if feedback.issue_space:
        new_wm = max(comment.id for comment in feedback.issue_space)
        if isinstance(cur_issue_wm, int):
            new_wm = max(new_wm, cur_issue_wm)
        state.set("pr_last_comment_id", new_wm)

    cur_review_wm = state.get("pr_last_review_comment_id")
    if feedback.review_comments:
        new_wm = max(comment.id for comment in feedback.review_comments)
        if isinstance(cur_review_wm, int):
            new_wm = max(new_wm, cur_review_wm)
        state.set("pr_last_review_comment_id", new_wm)

    cur_summary_wm = state.get("pr_last_review_summary_id")
    if feedback.review_summaries:
        new_wm = max(review.id for review in feedback.review_summaries)
        if isinstance(cur_summary_wm, int):
            new_wm = max(new_wm, cur_summary_wm)
        state.set("pr_last_review_summary_id", new_wm)


def _handle_continue_command(
    ctx: _FixingContext,
    feedback: _FixingFeedback,
) -> tuple:
    """Dispatch a `/orchestrator continue` operator command on a parked
    `fixing` issue.

    `/orchestrator continue` is the operator's "retry this fix" signal for a
    session-limit / session-failure park: a dev session that went silent
    (`agent_silent`) or timed out (`agent_timeout`) and left the fix-loop
    parked. The naive un-park resumes the dev on the command text alone,
    dropping the PR review feedback the poisoned session never addressed --
    the geserdugarov/lance-open-source#23 shape.

    Returns `(action, items)`:

      * ``("replay", batch)`` -- an eligible park WITH a reconstructable batch
        (the in_review `pending_fix_*` bookmarks, or the validating-route
        `pending_fix_reviewer_comment_id` anchor). Drops the poisoned dev
        session (so the retry re-grounds a FRESH session on the committed
        branch rather than replaying the transcript that already failed) and
        clears the park, as side effects; `batch` is the preserved PR-feedback
        batch (`_reconstruct_pending_fix_batch`) followed by ALL fresh feedback
        verbatim -- the command comment AND any guidance posted with or beside
        it -- so nothing the operator wrote is dropped. Pinned state is NOT
        written here (the caller's resume tail writes it).
      * ``("refuse", None)`` -- a content-free continue (every fresh comment is
        a bare command) on a park it cannot retry: an unsafe park that still
        needs real human guidance, or an eligible park with no reconstructable
        batch (a validating-route park whose reviewer anchor was never recorded
        or has since been deleted). Consumes the command comment (so the
        refusal does not re-fire) and posts the reason; the caller writes state
        and the issue stays parked.
      * ``("passthrough", None)`` -- the command arrived alongside genuine
        guidance on a park with no replayable batch. No side effect; the caller
        runs the normal resume so that guidance (not a bare continue) drives
        the dev.
    """
    from orchestrator import workflow as _wf

    park_reason = ctx.state.get(_PARK_REASON)
    batch = (
        _reconstruct_pending_fix_batch(ctx.gh, ctx.issue, ctx.pr, ctx.state)
        if park_reason in _wf._CONTINUE_PARK_REASONS else []
    )
    if batch:
        _wf._drop_poisoned_dev_session(ctx.state)
        ctx.state.set(_AWAITING_HUMAN, False)
        ctx.state.set(_PARK_REASON, None)
        _wf.log.info(
            "issue=#%s /orchestrator continue: replaying %d preserved feedback "
            "item(s) on a fresh dev session (park_reason=%s)",
            ctx.issue.number, len(batch), park_reason,
        )
        # Carry every fresh comment (command + any accompanying guidance)
        # verbatim into the replay: the resume tail advances the watermarks
        # past all of `feedback`, so anything omitted here would be consumed
        # without the dev ever seeing it.
        return "replay", batch + feedback.all_items

    if all(
        _wf._is_bare_orchestrator_continue(comment)
        for comment in feedback.all_items
    ):
        # Content-free continue with nothing else to act on. Consume only the
        # command comment(s) (`feedback.all_items` is all bare commands here,
        # so `continue_cmds` covers them) so the refusal is not re-posted every
        # tick, then stay parked with a reason.
        continue_cmds = _wf._parse_orchestrator_continue(feedback.issue_space)
        command_feedback = _FixingFeedback(
            issue_space=continue_cmds,
            review_comments=[],
            review_summaries=[],
            all_items=continue_cmds,
        )
        _advance_consumed_watermarks(ctx.state, command_feedback)
        if park_reason in _wf._CONTINUE_PARK_REASONS:
            message = (
                f"{config.HITL_MENTIONS} `/orchestrator continue`: no "
                "preserved PR-feedback batch is on file to replay for this "
                "park. Reply with the change to make, or relabel the issue, "
                "to proceed."
            )
        else:
            message = (
                f"{config.HITL_MENTIONS} `/orchestrator continue` needs your "
                "actual guidance here: this park is waiting on a real answer "
                "(an agent question, or a worktree it could not finish), not "
                "a generic continue. Reply with the specific change to make, "
                "or relabel the issue, to proceed."
            )
        _wf._post_issue_comment(ctx.gh, ctx.issue, ctx.state, message)
        return "refuse", None

    # The command came WITH genuine guidance on a park with no replayable
    # batch; let the normal resume feed that guidance to the dev.
    return "passthrough", None
