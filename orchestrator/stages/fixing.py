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
    `pending_fix_at`; the dev runs inline in the same tick and the
    validating handler flips back to `validating` itself on a pushed
    fix with `review_round` bumped. Only the parked outcomes (timeout
    / no-commit / dirty / push-fail) leave the fixing handler to own
    the awaiting-human cycle here.

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
handler rechecks `park_reason`, and on the in_review route (the only route
that preserves a replayable batch) drops the poisoned dev session so the
retry re-grounds a fresh one on the committed branch and replays the
PRESERVED feedback batch reconstructed from the `pending_fix_*` bookmarks,
carrying ALL fresh feedback (the command comment and any guidance posted
with or beside it) verbatim so nothing the operator wrote is dropped. The
command is handled on BOTH routes so a validating-route session-failure
park is never resumed on the bare command text either; with no batch to
replay there, a content-free continue is refused while a continue that
came WITH genuine guidance falls through to the normal resume so that
guidance drives the dev. Parks that still need real human guidance -- a
genuine agent question or a dirty worktree (both `park_reason=None`), or
any eligible park with no reconstructable batch -- refuse a content-free
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

import re
from datetime import datetime, timezone
from typing import Optional

from github.Issue import Issue

from .. import config
from ..config import RepoSpec
from ..state_machine import WorkflowLabel
from ..github import GitHubClient


# Park reasons `/orchestrator continue` may retry: a dev session that went
# silent (`agent_silent`, a poisoned resume that produced no output) or timed
# out (`agent_timeout`). Both are session-limit / session-failure conditions
# whose recovery is "retry the fix on a fresh session", not "wait for a human
# answer". Every other awaiting-human shape -- a real agent question or a
# dirty worktree (both `park_reason=None`), a stuck push, a missing PR --
# needs the human's actual guidance, so the command is refused there.
_CONTINUE_PARK_REASONS = frozenset({"agent_silent", "agent_timeout"})

# `/orchestrator continue` operator command, matched as an EXACT LINE
# (anchored to line boundaries, mirrors `_ADD_REVIEW_ROUNDS_RE` in
# `stages.validating`) so prose that mentions the command in backticks
# cannot fire it. `search` detects the command line inside a larger comment
# -- so a comment that carries the command AND real guidance still counts as
# the command -- while `_is_bare_orchestrator_continue` (whole stripped body
# == the line) tells a content-free nudge apart from a comment that also
# carries guidance, which governs whether an un-replayable park is refused or
# resumed on that guidance.
_ORCHESTRATOR_CONTINUE_RE = re.compile(
    r"^[ \t]*/orchestrator[ \t]+continue[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_orchestrator_continue(comments: list) -> list:
    """Return the comments whose body contains an exact-line
    `/orchestrator continue` operator command."""
    return [
        c for c in comments if _ORCHESTRATOR_CONTINUE_RE.search(c.body or "")
    ]


def _is_bare_orchestrator_continue(comment) -> bool:
    """True when the comment's ENTIRE body is the command line and nothing
    else -- a content-free nudge whose consumption drops no guidance."""
    return (
        _ORCHESTRATOR_CONTINUE_RE.fullmatch((comment.body or "").strip())
        is not None
    )


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
    #
    # `replay_batch` is set only by an accepted `/orchestrator continue`
    # command below: the PRESERVED PR-feedback batch (plus any genuinely new
    # feedback that arrived with the command) to resume the fresh dev on,
    # instead of the per-tick rescan. It skips the debounce and re-grounds a
    # dropped session further down.
    replay_batch: Optional[list] = None
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

        # `/orchestrator continue` operator command (exact line, so a comment
        # carrying the command AND real guidance still counts). Handled on
        # BOTH routes so a session-failure park (`agent_silent` /
        # `agent_timeout`) never resumes the dev on the bare command text.
        # `_handle_continue_command` decides:
        #   * "replay" -- eligible park with a reconstructable batch (the
        #     in_review route): the poisoned session is dropped and the park
        #     cleared, and we resume the fresh dev on the preserved batch (see
        #     below), skipping the debounce.
        #   * "refuse" -- a content-free continue on a park it cannot retry
        #     (an unsafe park needing real guidance, or an eligible park with
        #     no batch, e.g. the validating route): command consumed + reason
        #     posted; stay parked.
        #   * "passthrough" -- the command arrived WITH genuine guidance on a
        #     park with no replayable batch: fall through to the normal resume
        #     so that guidance (not a bare continue) drives the dev.
        continue_cmds = _parse_orchestrator_continue(issue_space_new)
        if continue_cmds:
            action, items = _handle_continue_command(
                gh, issue, state, pr, park_reason, continue_cmds,
                new_feedback, issue_space_new, review_space_new,
                review_summary_new,
            )
            if action == "refuse":
                gh.write_pinned_state(issue, state)
                return
            if action == "replay":
                replay_batch = items
            # "passthrough": fall through to the normal resume below.

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
    # `created_at` / `submitted_at` are always set. An accepted
    # `/orchestrator continue` (`replay_batch` set) skips the wait
    # entirely -- it is a deliberate operator signal, not chatter to
    # debounce.
    now = datetime.now(timezone.utc)
    latest_ts: Optional[datetime] = None
    for c in new_feedback:
        ts = _wf._comment_created_at(c)
        if ts is None:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
    if (
        replay_batch is None
        and latest_ts is not None
        and (now - latest_ts).total_seconds() < config.IN_REVIEW_DEBOUNCE_SECONDS
    ):
        return

    # On an accepted `/orchestrator continue`, resume on the PRESERVED batch
    # (plus any new feedback that came with the command), not the command
    # text -- the whole point of the command is to not lose the review
    # feedback the parked session never addressed.
    followup = _wf._build_pr_comment_followup(
        replay_batch if replay_batch is not None else new_feedback
    )
    wt = _wf._worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _wf._ensure_worktree(
            spec, issue.number,
            branch=_wf._resolve_branch_name(state, spec, issue.number),
        )
    before_sha = _wf._head_sha(wt)
    wt, dev_result, paused = _wf._resume_dev_with_text(
        gh, spec, issue, state, followup, pause_guard=True,
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
    if dev_result.interrupted:
        return

    # Live pause applied while the agent ran: an operator added `paused` (or
    # `backlog`) mid-run. Honor the decision `_resume_dev_with_text` already
    # made (propagated, not re-fetched) and stop before the ACK fast path, the
    # stranded-fix publish, `_handle_dev_fix_result`, the watermark advance, or
    # any relabel / pinned-state write. The committed work stays on the branch,
    # so once the label is removed the normal recovered / stranded-fix path
    # republishes it.
    if paused:
        return

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
    dirty (an operator may be inspecting a dirty-tree park), or the
    worktree is already in sync with the PR head (the transient
    condition is the real blocker, not drift).

    The `pending_fix_*` bookmarks and in_review watermarks are left
    untouched so the eventual `in_review` re-entry still re-discovers the
    feedback (mirrors the refresh-time conflict detour).
    """
    from .. import workflow as _wf

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
    state.set("pending_fix_issue_ids", None)
    state.set("pending_fix_review_ids", None)
    state.set("pending_fix_review_summary_ids", None)


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
        return {int(i) for i in ids}
    max_id = state.get(max_id_key)
    if isinstance(max_id, int) and not isinstance(max_id, bool):
        return {max_id}
    return set()


def _reconstruct_pending_fix_batch(gh, issue, pr, state) -> list:
    """Rebuild the exact feedback batch that drove the `in_review` -> `fixing`
    route from the pinned `pending_fix_*` metadata.

    The per-tick rescan in `_handle_fixing` reads from the in_review
    watermarks, which advance past the triggering feedback the moment a dev
    resume consumes it -- so once a fix has been attempted the batch can no
    longer be recovered by rescanning. This helper reconstructs it from the
    persisted ids instead: it re-fetches every surface in full
    (`after_id=None`) and keeps only the items whose id was recorded at
    route time, returned in the same order the route built them --
    issue-space (issue-thread + PR-conversation, one shared IssueComment id
    space) then inline review comments then review summaries, each sorted by
    id. Filtering by the recorded id set inherently drops the orchestrator's
    own comments (their ids were never in the batch) and survives watermark
    advancement because the fetch is unbounded.

    Existing parked issues that carry only `pending_fix_*_max_id` (no id
    lists) get the conservative single-item reconstruction from
    `_pending_fix_id_set`. A batch item deleted on GitHub since the route
    simply drops out -- it cannot be reconstructed and is not worth
    special-casing.
    """
    issue_ids = _pending_fix_id_set(
        state, "pending_fix_issue_ids", "pending_fix_issue_max_id",
    )
    review_ids = _pending_fix_id_set(
        state, "pending_fix_review_ids", "pending_fix_review_max_id",
    )
    summary_ids = _pending_fix_id_set(
        state,
        "pending_fix_review_summary_ids",
        "pending_fix_review_summary_max_id",
    )

    issue_space: list = []
    if issue_ids:
        for c in gh.comments_after(issue, None):
            if c.id in issue_ids:
                issue_space.append(c)
        for c in gh.pr_conversation_comments_after(pr, None):
            if c.id in issue_ids:
                issue_space.append(c)
        issue_space.sort(key=lambda c: c.id)

    review_space: list = []
    if review_ids:
        review_space = [
            c for c in gh.pr_inline_comments_after(pr, None)
            if c.id in review_ids
        ]
        review_space.sort(key=lambda c: c.id)

    review_summary: list = []
    if summary_ids:
        review_summary = [
            r for r in gh.pr_reviews_after(pr, None)
            if r.id in summary_ids
        ]
        review_summary.sort(key=lambda r: r.id)

    return issue_space + review_space + review_summary


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


def _handle_continue_command(
    gh,
    issue,
    state,
    pr,
    park_reason,
    continue_cmds: list,
    new_feedback: list,
    issue_space_new: list,
    review_space_new: list,
    review_summary_new: list,
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

      * ``("replay", batch)`` -- an eligible park WITH a reconstructable
        batch (the in_review route). Drops the poisoned dev session (so the
        retry re-grounds a FRESH session on the committed branch rather than
        replaying the transcript that already failed) and clears the park, as
        side effects; `batch` is the preserved PR-feedback batch
        (`_reconstruct_pending_fix_batch`) followed by ALL fresh feedback
        verbatim -- the command comment AND any guidance posted with or beside
        it -- so nothing the operator wrote is dropped. Pinned state is NOT
        written here (the caller's resume tail writes it).
      * ``("refuse", None)`` -- a content-free continue (every fresh comment
        is a bare command) on a park it cannot retry: an unsafe park that
        still needs real human guidance, or an eligible park with no batch to
        replay (the validating route, whose triggering reviewer verdict is
        not preserved as a batch). Consumes the command comment (so the
        refusal does not re-fire) and posts the reason; the caller writes
        state and the issue stays parked.
      * ``("passthrough", None)`` -- the command arrived alongside genuine
        guidance on a park with no replayable batch. No side effect; the
        caller runs the normal resume so that guidance (not a bare continue)
        drives the dev.
    """
    from .. import workflow as _wf

    batch = (
        _reconstruct_pending_fix_batch(gh, issue, pr, state)
        if park_reason in _CONTINUE_PARK_REASONS else []
    )
    if batch:
        _wf._drop_poisoned_dev_session(state)
        state.set("awaiting_human", False)
        state.set("park_reason", None)
        _wf.log.info(
            "issue=#%s /orchestrator continue: replaying %d preserved feedback "
            "item(s) on a fresh dev session (park_reason=%s)",
            issue.number, len(batch), park_reason,
        )
        # Carry every fresh comment (command + any accompanying guidance)
        # verbatim into the replay: the resume tail advances the watermarks
        # past all of `new_feedback`, so anything omitted here would be
        # consumed without the dev ever seeing it.
        return "replay", batch + new_feedback

    if all(_is_bare_orchestrator_continue(c) for c in new_feedback):
        # Content-free continue with nothing else to act on. Consume only the
        # command comment(s) (`new_feedback` is all bare commands here, so this
        # covers them) so the refusal is not re-posted every tick, then stay
        # parked with a reason.
        _advance_consumed_watermarks(state, list(continue_cmds), [], [])
        if park_reason in _CONTINUE_PARK_REASONS:
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
        _wf._post_issue_comment(gh, issue, state, message)
        return "refuse", None

    # The command came WITH genuine guidance on a park with no replayable
    # batch; let the normal resume feed that guidance to the dev.
    return "passthrough", None
