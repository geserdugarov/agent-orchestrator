# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fixing stage handler.

`_handle_fixing` owns the PR-feedback quiet window and the dev-resume /
push / hand-back-to-`validating` cycle. `_handle_in_review` flips the
label to `fixing` the moment fresh PR feedback (issue-thread,
PR-conversation, inline-review, or review-summary) is detected; the
in_review handler deliberately leaves the in_review watermarks behind
so this handler can read the triggering comments for its dev-resume
prompt.

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
the bookmarks, resets `review_round`, and flips the label DIRECTLY back
to `validating` so the reviewer agent re-evaluates the freshened diff
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
from ..github import GitHubClient


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
    if state.get("awaiting_human"):
        if not new_feedback:
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
        gh.set_workflow_label(issue, "validating")
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
        wt = _wf._ensure_worktree(spec, issue.number)
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

    pushed = _wf._handle_dev_fix_result(
        gh, spec, issue, state, wt, dev_result, before_sha,
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

    # Reset the round so the reviewer starts fresh on the new diff
    # next tick. The bookmarks served their purpose; clear them so a
    # later in_review -> fixing route writes fresh values rather than
    # mixing rounds.
    #
    # Flip DIRECTLY to `validating` so the reviewer re-evaluates the
    # new head next tick. Docs do not run on this exit -- the single
    # docs pass is deferred to the final-docs handoff after reviewer
    # approval, so running the docs stage against an unapproved diff
    # here would just push a no-op and waste a tick.
    _clear_pending_fix_bookmarks(state)
    state.set("review_round", 0)
    gh.set_workflow_label(issue, "validating")
    gh.write_pinned_state(issue, state)


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
