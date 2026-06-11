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
    per-namespace `pending_fix_*_max_id` bookmarks.
  * `_handle_validating` flips it BEFORE spawning the dev on a
    `CHANGES_REQUESTED` verdict so the dev-fix subphase is observably
    labeled `fixing` (the active job is "fixing reviewer-requested
    changes", not "validating"). This route does NOT set
    `pending_fix_at`; the dev runs inline in the same tick and the
    validating handler flips back to `validating` itself on a pushed
    fix with `review_round` bumped. Only the parked outcomes (timeout
    / no-commit / dirty / push-fail) leave the fixing handler to own
    the awaiting-human cycle here.

Each tick the handler rescans unread feedback from the existing watermarks
(NOT the `pending_fix_*_max_id` bookmarks recorded by the route -- those
remain in pinned state as forensic hints). Newer comments arriving while
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
helper no-ops when `hold_base_sync` is set, the worktree is missing /
dirty, or the worktree is already in sync with the PR head.

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

from datetime import datetime, timezone
from typing import Optional

from github.Issue import Issue

from .. import config
from ..config import RepoSpec
from ..state_machine import WorkflowLabel
from ..github import BASE_SYNC_HOLD_LABEL, GitHubClient, issue_has_label


def _handle_fixing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)
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
    # limit, 5xx). Catch and bail with `pr=None` so the rescan below
    # also short-circuits via the `if pr is None: return` guard --
    # the next tick re-fetches and picks up wherever we left off; the
    # watermarks are unchanged so no feedback is lost.
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
            return

    # Closed issue with no PR (or a PR lookup failure): nothing to
    # finalize via the PR-state arcs above. Leave alone rather than
    # parking a closed issue.
    if getattr(issue, "state", "open") == "closed":
        _wf.log.info(
            "repo=%s issue=#%s closed fixing issue with no resolvable PR; "
            "leaving alone (relabel manually to finalize)",
            spec.slug, issue.number,
        )
        return

    if pr_number is None:
        # `fixing` is only ever entered with a recorded PR (in_review
        # holds the PR before routing). Reaching here means a manual
        # relabel from outside that route -- park once and surface to a
        # human; the dev-resume path needs the PR to push a fix.
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `fixing` without a pinned "
            "`pr_number`; manual relabeling suspected. Set the workflow "
            "label back to `in_review` (or `validating`) after attaching "
            "a PR.",
            reason="missing_pr_number",
        )
        gh.write_pinned_state(issue, state)
        return

    # `pr_number` was set but `gh.get_pr` raised above. The exception is
    # already logged; bail this tick so the rescan below does not
    # dereference `None`. PyGithub failures here are typically transient
    # (network blip, rate limit, 5xx), so the next tick re-fetches and
    # picks up wherever we left off; the watermarks are unchanged so no
    # feedback is lost.
    if pr is None:
        return

    # Mirror `_handle_in_review`'s fallback: if no PR-side watermark
    # exists yet (an in_review tick that routed to `fixing` before
    # ever seeding `pr_last_comment_id` -- e.g. a manual relabel into
    # `in_review` without going through validating, or a legacy issue
    # that pre-dates the watermark migration), fall back to
    # `last_action_comment_id`. Without this, `comments_after` /
    # `pr_conversation_comments_after` would be called with `after_id=None`
    # and re-feed every historical issue / PR-conversation comment into
    # the dev's `_build_pr_comment_followup` prompt as fresh feedback.
    # Capture `pending_fix_at` BEFORE the bookmark-clear branches below.
    # It distinguishes the in_review->fixing route (set by the in_review
    # handler when fresh PR feedback lands) from the validating->fixing
    # route (set when a CHANGES_REQUESTED dev fix parks). The pushed-fix
    # branch resets `review_round` to 0 only for the in_review route --
    # there, the previous reviewer round was APPROVED so the next round
    # starts fresh. For validating->fixing, the previous round was
    # CHANGES_REQUESTED and we're still inside the same review cycle, so
    # the round must be bumped, not reset (otherwise MAX_REVIEW_ROUNDS
    # accounting silently restarts when a parked CHANGES_REQUESTED fix
    # is finished off via a human reply).
    pending_fix_at_was_set = state.get("pending_fix_at") is not None

    issue_wm = state.get("pr_last_comment_id")
    if issue_wm is None:
        issue_wm = state.get("last_action_comment_id")
    review_wm = state.get("pr_last_review_comment_id")
    review_summary_wm = state.get("pr_last_review_summary_id")
    orchestrator_ids = _wf._orchestrator_ids(state)
    # Issue and PR-conversation comments share the IssueComment id
    # namespace, so the same watermark covers both. Filter orchestrator
    # comments by id AND by the hidden body marker -- the id-cap evicts
    # old ids on long-lived issues, after which an id-only filter would
    # start re-feeding old bot comments to the dev.
    new_issue_side = [
        c for c in gh.comments_after(issue, issue_wm)
        if c.id not in orchestrator_ids
        and _wf._ORCH_COMMENT_MARKER not in (c.body or "")
    ]
    new_pr_conv = [
        c for c in gh.pr_conversation_comments_after(pr, issue_wm)
        if c.id not in orchestrator_ids
        and _wf._ORCH_COMMENT_MARKER not in (c.body or "")
    ]
    # Inline review comments and review summaries live in their own id
    # spaces; the orchestrator never posts on those surfaces so no
    # filter is needed.
    new_pr_inline = list(gh.pr_inline_comments_after(pr, review_wm))
    new_pr_reviews = list(gh.pr_reviews_after(pr, review_summary_wm))
    issue_space_new = sorted(
        list(new_issue_side) + list(new_pr_conv), key=lambda c: c.id,
    )
    review_space_new = sorted(new_pr_inline, key=lambda c: c.id)
    review_summary_new = sorted(new_pr_reviews, key=lambda r: r.id)
    new_feedback = issue_space_new + review_space_new + review_summary_new

    # Parked from a prior failed resume: bail unless something new has
    # arrived since the bump that followed the park. The watermarks were
    # advanced past the previously-consumed feedback, so `new_feedback`
    # here can only contain genuinely new content (a human reply, a fresh
    # inline review, a follow-up summary). Without this guard a single
    # poisoned tick would loop on every poll until human intervention,
    # spamming the same dev-resume prompt at the agent.
    #
    # Exception: when the park reason can resolve without a human comment
    # AND the issue arrived here via the validating route (CHANGES_
    # REQUESTED dev fix), attempt silent recovery first. The
    # `_handle_validating` CHANGES_REQUESTED branch flips to `fixing`
    # BEFORE spawning the dev, so a transient park (`push_failed` /
    # `agent_timeout`) lands under `fixing` instead of `validating`;
    # without this recovery branch the issue would sit forever in
    # `fixing` awaiting a human comment the underlying condition does
    # not produce. The shared `_try_recover_validating_transient_park`
    # helper (re-exported from `workflow`) implements the dev-side
    # reconcile and round bookkeeping.
    #
    # The route discriminator is `pending_fix_at`: the in_review route
    # sets it when fresh human PR feedback drives the relabel, while the
    # validating route leaves it unset. Recovery must NOT run on the
    # in_review route because:
    #
    #   * `_handle_fixing` advances the PR-feedback watermarks past the
    #     human comment even on a timed-out dev resume (so the dev does
    #     not replay it). A subsequent silent recovery that clears
    #     `agent_timeout` and bounces back to `validating` would consume
    #     the human's PR feedback without ever applying a fix.
    #   * The shared helper bumps `review_round` on its `pushed` outcome.
    #     The in_review route resets `review_round` to 0 on a pushed fix
    #     (the previous reviewer round was APPROVED, so a new cycle
    #     starts fresh), so the shared helper would mis-account the
    #     round when a deferred push lands on this route.
    #
    # On the in_review route a transient park therefore stays parked
    # until a human comment arrives, matching the original behavior
    # (this code path had no transient recovery before -- the validating
    # handler held that responsibility for parks under `validating`).
    if state.get("awaiting_human"):
        park_reason = state.get("park_reason")
        # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to
        # the `_sync_pr_worktree_to_base` retry loop -- the operator's
        # new comment is the "retry the rebase" signal, NOT fresh PR
        # feedback for the dev fix-loop. Stay silent so the refresh
        # keeps ownership of the comment; resuming the dev here would
        # spawn it on a prompt that has nothing to do with the
        # outstanding fix.
        if park_reason in _wf._AUTO_REBASE_PARK_REASONS:
            return
        validating_routed = state.get("pending_fix_at") is None
        if (
            not new_feedback
            and park_reason in _wf._VALIDATING_TRANSIENT_PARK_REASONS
            and validating_routed
        ):
            recovery = _wf._try_recover_validating_transient_park(
                spec, issue, state,
            )
            if recovery == "stuck":
                # The transient condition has not resolved on its own
                # (e.g. `push_failed` keeps failing). When the worktree
                # has drifted from the PR head in the meantime, hand the
                # reconciliation to `resolving_conflict` rather than sit
                # parked forever -- the per-tick base sync deliberately
                # stands down on every `awaiting_human` park, so nobody
                # else will sync this worktree. Limiting the drift route
                # to this branch keeps the HITL contract intact: question
                # / dirty / silent / in_review-route transient parks fall
                # through to the bare `return` below and keep waiting for
                # a human comment.
                _reconcile_parked_fixing(gh, spec, issue, state, pr)
                return
            # Conditions resolved (either no fix landed or a deferred
            # push finished). Clear the park flags and flip back to
            # `validating` so the reviewer re-evaluates the current head
            # next tick. The helper has already bumped `review_round`
            # when a fix landed (push_failed, or agent_timeout that
            # finished its push). Clear the pending_fix_* bookmarks
            # defensively: this branch ONLY fires when `pending_fix_at`
            # was already None, so the clear is a no-op in normal flow,
            # but a stale bookmark from an earlier route would otherwise
            # mis-flag the next reviewer round.
            state.set("awaiting_human", False)
            state.set("park_reason", None)
            _clear_pending_fix_bookmarks(state)
            gh.set_workflow_label(issue, WorkflowLabel.VALIDATING)
            gh.write_pinned_state(issue, state)
            return
        if not new_feedback:
            # All other awaiting_human shapes (question parks, dirty
            # worktree parks, silent-crash parks, in_review-route
            # transients) stay parked until a fresh human reply lands.
            # We cannot distinguish "agent has a real question" from
            # "agent reported nothing to change" by inspection -- both
            # surface through `_on_question` with `park_reason=None` --
            # so auto-routing either would silently bypass the HITL
            # contract. The same applies to a clean in-sync worktree on
            # the in_review route: the dev may have replied with a real
            # question that needs a human to resolve, so the only
            # automatic exit from `fixing` for the in_review route is
            # the ACK fast path below (on the same tick the dev
            # explicitly marks its no-commit reply with `ACK:`).
            return
        state.set("awaiting_human", False)
        state.set("park_reason", None)

    # Watermarks already cover the triggering bookmarks (a prior tick
    # consumed them, or an operator advanced them manually). Nothing
    # left to address; clear the route bookkeeping and bounce back to
    # `validating` so the reviewer re-evaluates against the current
    # head instead of leaving the issue stuck in `fixing` with no
    # work.
    if not new_feedback:
        _clear_pending_fix_bookmarks(state)
        gh.set_workflow_label(issue, WorkflowLabel.VALIDATING)
        gh.write_pinned_state(issue, state)
        return

    # Quiet window: hold the resume until no comment has landed for
    # `IN_REVIEW_DEBOUNCE_SECONDS`. A newer comment arriving on a
    # later tick is naturally picked up by the rescan above, which
    # extends the wait because the freshest timestamp controls the
    # gate. Comments without a usable timestamp (older fakes,
    # PyGithub edge cases) do not block the resume; in production
    # `created_at` / `submitted_at` are always set.
    now = datetime.now(timezone.utc)
    latest_ts: Optional[datetime] = None
    for c in new_feedback:
        ts = _wf._comment_created_at(c)
        if ts is None:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
    if (
        latest_ts is not None
        and (now - latest_ts).total_seconds() < config.IN_REVIEW_DEBOUNCE_SECONDS
    ):
        return

    followup = _wf._build_pr_comment_followup(new_feedback)
    wt = _wf._worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _wf._ensure_worktree(
            spec, issue.number,
            branch=_wf._resolve_branch_name(state, spec, issue.number),
        )
    before_sha = _wf._head_sha(wt)
    wt, dev_result = _wf._resume_dev_with_text(
        gh, spec, issue, state, followup,
    )
    state.set("last_agent_action_at", _wf._now_iso())

    # Refresh the user-content drift hash to include any human
    # issue-thread comments we just fed to the dev via `followup`.
    # Without this, the next tick that runs `_handle_validating` (or
    # any other handler that calls `_detect_user_content_change`)
    # would see those consumed comments as fresh user-content drift
    # and resume the dev a second time on input it has already
    # handled. Mirrors the hash refresh `_handle_in_review` does at
    # the moment it routes to `fixing`. Refresh on BOTH success and
    # failure paths: the dev saw the comments via the prompt either
    # way, so the baseline must move with the consumption regardless
    # of whether the agent pushed a fix this tick.
    state.set(
        "user_content_hash",
        _wf._compute_user_content_hash(
            issue, _wf._orchestrator_ids(state),
        ),
    )

    # Read HEAD only when the run did not time out -- the timeout branch of
    # `_handle_dev_fix_result` returns before it would use `after_sha`, and
    # reading here would burn an extra `_head_sha` the timeout path never did.
    after_sha = None if dev_result.timed_out else _wf._head_sha(wt)

    # ACK fast path (in_review route only): the dev made no commit but
    # explicitly signaled via the `ACK: <reason>` marker that the PR
    # feedback carries no actionable change. A vague "continue" / "ok"
    # nudge should not strand a complete, mergeable PR in `fixing`, so
    # return to `in_review` (re-arming the ready-ping) instead of parking.
    # The validating CHANGES_REQUESTED route (`pending_fix_at` unset) is
    # excluded -- the reviewer DID request a concrete change, so an ACK
    # there falls through to `_handle_dev_fix_result`, which parks for
    # the human unless its stranded-fix check finds the clean HEAD
    # already strictly ahead of the remote PR branch and publishes that
    # committed-but-unpushed fix instead
    # (`validating._stranded_fix_unpushed`).
    #
    # The fast path itself stands down on the same stranded shape: the
    # ack vouches for the *feedback*, not for the publish state, so when
    # the clean HEAD is strictly ahead of the remote PR branch (a fix a
    # prior parked run committed but never pushed -- e.g. a dirty-park
    # whose stray files were later cleaned up) relabeling to `in_review`
    # here would clear the bookmarks, advance the watermarks, and present
    # a PR head that is still missing the committed fix. Falling through
    # lets `_handle_dev_fix_result` publish the stranded HEAD through its
    # normal push tail and the pushed-fix exit below route the freshened
    # head back to the reviewer. The check is skipped when `after_sha`
    # is unreadable (mirrors `_handle_dev_fix_result`'s own gate -- no
    # pushing blind off a worktree whose HEAD we could not read).
    if (
        pending_fix_at_was_set
        and not dev_result.timed_out
        and (not after_sha or after_sha == before_sha)
    ):
        ack_reason = _wf._drift_ack_reason(dev_result.last_message or "")
        if ack_reason and not (
            after_sha and _wf._stranded_fix_unpushed(spec, wt, state, issue)
        ):
            _advance_consumed_watermarks(
                state, issue_space_new, review_space_new, review_summary_new,
            )
            _clear_pending_fix_bookmarks(state)
            quoted = "> " + ack_reason.replace("\n", "\n> ")
            _wf._post_issue_comment(
                gh, issue, state,
                ":speech_balloon: dev session reports the PR feedback needs "
                f"no change:\n\n{quoted}\n\nReturning to `in_review`.",
            )
            # The session is alive and producing a coherent ack, so reset
            # the silent-park streak (mirrors the drift-ack handling).
            state.set("silent_park_count", 0)
            gh.set_workflow_label(issue, WorkflowLabel.IN_REVIEW)
            gh.write_pinned_state(issue, state)
            return

    pushed = _wf._handle_dev_fix_result(
        gh, spec, issue, state, wt, dev_result, before_sha, after_sha=after_sha,
    )

    # Advance the three in_review watermarks ONLY to the max id actually
    # fed to the dev on each surface (ratcheted against the current
    # watermark). Deliberately tighter than `_bump_in_review_watermarks`,
    # which also pulls in `gh.latest_comment_id(issue)`: a human
    # issue-thread comment that landed AFTER `new_feedback` was built
    # but BEFORE this write was never quoted in the dev's
    # `_build_pr_comment_followup` prompt, so silently moving the
    # watermark past it would swallow real feedback.
    #
    # This applies to BOTH paths:
    #
    #   * On a pushed fix, the next in_review tick (after `validating`
    #     completes) must rediscover the concurrent comment as fresh PR
    #     feedback.
    #
    #   * On park/failure (timeout / dirty / push fail / no-commit), the
    #     next fixing tick must also rediscover it -- otherwise the
    #     `awaiting_human and not new_feedback` gate fires and the
    #     concurrent human comment is silently dropped, breaking the
    #     "comments arriving while already labeled `fixing`" contract on
    #     every failure mode.
    #
    # The orchestrator's own park comment posted by
    # `_park_awaiting_human` (issue id-space, body carries
    # `_ORCH_COMMENT_MARKER` and its id is recorded in
    # `orchestrator_comment_ids`) does NOT need a watermark bump to
    # avoid replay: the next tick's rescan filters by both id and body
    # marker, so the park comment is dropped even when the watermark
    # sits below it. The legacy in_review pushed-fix path had the same
    # constraint.
    _advance_consumed_watermarks(
        state, issue_space_new, review_space_new, review_summary_new,
    )

    if not pushed:
        gh.write_pinned_state(issue, state)
        return

    # Bookmarks served their purpose; clear them so a later
    # in_review->fixing route writes fresh values rather than mixing
    # rounds. The round update depends on which route brought us here
    # (see `pending_fix_at_was_set` above):
    #
    #   * in_review->fixing: reset to 0. The previous reviewer round
    #     was APPROVED (the in_review HITL ping is gated on approval);
    #     the new fix starts a fresh round-count so MAX_REVIEW_ROUNDS
    #     does not trip prematurely on issues that pass back through
    #     review after a human PR comment.
    #
    #   * validating->fixing (CHANGES_REQUESTED dev fix that parked and
    #     was finished via a human reply): bump. The previous round
    #     was CHANGES_REQUESTED, not APPROVED, so we are still in the
    #     same review cycle and the round counter must advance to keep
    #     MAX_REVIEW_ROUNDS accounting honest.
    #
    # Flip DIRECTLY to `validating` so the reviewer re-evaluates the
    # new head next tick. Docs do not run on this exit -- the single
    # docs pass is deferred to the final-docs handoff after reviewer
    # approval, so running the docs stage against an unapproved diff
    # here would just push a no-op and waste a tick.
    _clear_pending_fix_bookmarks(state)
    if pending_fix_at_was_set:
        state.set("review_round", 0)
    else:
        round_n = int(state.get("review_round") or 0)
        state.set("review_round", round_n + 1)
    gh.set_workflow_label(issue, WorkflowLabel.VALIDATING)
    gh.write_pinned_state(issue, state)


def _reconcile_parked_fixing(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state, pr,
) -> bool:
    """Hand a stuck validating-route transient `fixing` park to
    `resolving_conflict` on worktree drift.

    Called from the `recovery == "stuck"` branch of `_handle_fixing`:
    `_try_recover_validating_transient_park` could not clear the
    transient condition (e.g. `push_failed` keeps failing), but the
    underlying cause may be a base advance that landed while the issue
    was parked. The per-tick base sync (`_sync_pr_worktree_to_base`)
    deliberately stands down on every `awaiting_human` park, so the
    integration work nobody else will do is stranded and the issue sits
    parked forever. Two drift shapes both reconcile via
    `resolving_conflict`, which owns rebasing AND publishing a PR
    branch:

      * worktree BEHIND `<remote>/<base>` -> needs a rebase.
      * worktree already rebased locally but the rewrite was never pushed,
        so local HEAD differs from the (stale) remote PR head -> needs a
        force-publish (`_handle_resolving_conflict` recognizes an
        already-rebased worktree and publishes it instead of parking).

    Relabel to `resolving_conflict` so its handler reconciles either shape
    on the next tick. The routing decision is cheap: base drift is a local
    `rev-list HEAD..<remote>/<base>`, and the unpushed-rebase check
    compares local HEAD to `pr.head.sha` (the live remote head the handler
    already fetched this tick) -- no extra fetch here.

    Returns False (issue stays parked) when the worktree is missing,
    dirty (an operator may be inspecting a dirty-tree park), the issue
    carries `hold_base_sync` (an explicit operator pause on base
    integration), or the worktree is already in sync with the PR head
    (the transient condition is the real blocker, not drift).

    The `pending_fix_*` bookmarks and in_review watermarks are left
    untouched so the eventual `in_review` re-entry still re-discovers the
    feedback (mirrors the refresh-time conflict detour).
    """
    from .. import workflow as _wf

    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        return False

    wt = _wf._worktree_path(spec, issue.number)
    if not wt.exists():
        return False
    if _wf._worktree_dirty_files(wt):
        return False

    # Trust the once-per-tick base fetch `_refresh_base_and_worktrees`
    # ran before dispatch (mirrors `_sync_worktree_with_base`, which also
    # measures behind without re-fetching). A stale ref can only undercount
    # (stay parked) or, on the rare case the per-tick fetch failed,
    # overcount -- and `_handle_resolving_conflict` re-fetches before it
    # acts, so an overcount self-corrects.
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    behind_r = _wf._git("rev-list", "--count", f"HEAD..{base_ref}", cwd=wt)
    if behind_r.returncode != 0:
        return False
    try:
        behind = int((behind_r.stdout or "0").strip() or "0")
    except ValueError:
        return False

    if behind > 0:
        drift_reason = f"{behind} commit(s) behind `{base_ref}`"
    else:
        # On top of base: is the local branch out of sync with the PR
        # head? `pr` was fetched fresh this tick, so `pr.head.sha` is the
        # live remote head. A mismatch means the worktree carries a rebase
        # that was never pushed -- `_handle_resolving_conflict` republishes
        # it (over a stale, orchestrator-produced PR head).
        local_head = _wf._head_sha(wt) or ""
        pr_head = getattr(getattr(pr, "head", None), "sha", None) or ""
        if local_head and pr_head and local_head != pr_head:
            drift_reason = (
                f"already rebased onto `{base_ref}`, but the PR head "
                f"(`{pr_head[:8]}`) is stale (local `{local_head[:8]}`)"
            )
        else:
            return False  # in sync with the PR -> genuine dev question

    pr_number = int(state.get("pr_number"))
    # Seed `conflict_round` only when absent so a re-entry preserves the
    # cap counter (mirrors `_route_pr_worktree_to_resolving_conflict`).
    if state.get("conflict_round") is None:
        state.set("conflict_round", 0)
    state.set("awaiting_human", False)
    state.set("park_reason", None)
    try:
        _wf._post_pr_comment(
            gh, pr_number, state,
            f":mag: PR worktree is out of sync ({drift_reason}) and the `fixing` "
            "fix-loop is parked on a stuck transient condition that the "
            "self-recovery could not clear. Routing `fixing` -> "
            "`resolving_conflict` to reconcile the branch before the next "
            "reviewer round.",
        )
    except Exception:
        _wf.log.exception(
            "issue=#%s could not post worktree-drift reroute notice to PR #%s",
            issue.number, pr_number,
        )
    gh.emit_event(
        "conflict_round",
        issue_number=issue.number,
        stage="fixing",
        pr_number=pr_number,
        sha=getattr(getattr(pr, "head", None), "sha", None) or None,
        action="entered",
        conflict_round=int(state.get("conflict_round") or 0),
        review_round=int(state.get("review_round") or 0),
        retry_count=state.get("retry_count"),
    )
    _wf.log.info(
        "issue=#%s parked `fixing` worktree is out of sync (%s); routing -> "
        "resolving_conflict",
        issue.number, drift_reason,
    )
    gh.set_workflow_label(issue, WorkflowLabel.RESOLVING_CONFLICT)
    gh.write_pinned_state(issue, state)
    return True


def _clear_pending_fix_bookmarks(state) -> None:
    state.set("pending_fix_at", None)
    state.set("pending_fix_issue_max_id", None)
    state.set("pending_fix_review_max_id", None)
    state.set("pending_fix_review_summary_max_id", None)


def _advance_consumed_watermarks(
    state,
    issue_space_new: list,
    review_space_new: list,
    review_summary_new: list,
) -> None:
    """Advance the three in_review watermarks ONLY to the max id consumed
    per surface, ratcheted against the existing watermark.

    Called once on every dev-result outcome (BOTH the pushed-fix path
    AND the park/failure path) before the pushed/non-pushed split, so
    a concurrent human comment that landed between `new_feedback` and
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
    if issue_space_new:
        new_wm = max(c.id for c in issue_space_new)
        if isinstance(cur_issue_wm, int):
            new_wm = max(new_wm, cur_issue_wm)
        state.set("pr_last_comment_id", new_wm)

    cur_review_wm = state.get("pr_last_review_comment_id")
    if review_space_new:
        new_wm = max(c.id for c in review_space_new)
        if isinstance(cur_review_wm, int):
            new_wm = max(new_wm, cur_review_wm)
        state.set("pr_last_review_comment_id", new_wm)

    cur_summary_wm = state.get("pr_last_review_summary_id")
    if review_summary_new:
        new_wm = max(r.id for r in review_summary_new)
        if isinstance(cur_summary_wm, int):
            new_wm = max(new_wm, cur_summary_wm)
        state.set("pr_last_review_summary_id", new_wm)
