# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating stage handlers and reviewer-session lifecycle.

Owns `_handle_validating` plus the reviewer-side primitives the rest of
the workflow re-uses: post-agent dev-fix disposition (`_handle_dev_fix_result`),
post-resume disposition for a user-content-change dev resume
(`_post_user_content_change_result`), the validating-side transient-park
recovery (`_try_recover_validating_transient_park` plus its
`_VALIDATING_TRANSIENT_PARK_REASONS` set), and the validating->in_review
handoff watermark seeding (`_seed_watermark_past_self`,
`_latest_pr_comment_ids`).

ALL workflow-owned helpers (`_park_awaiting_human`, `_run_agent_tracked`,
`_now_iso`, the worktree plumbing, the drift / manifest / messaging
helpers re-exported into `workflow`) are reached through the parent
module via `from .. import workflow as _wf` at call time. The
compatibility surface tests rely on -- `patch.object(workflow, "_foo")`
-- has to keep working from inside the stage module too, so the
handlers must NOT direct-import these names from `workflow_drift` /
`workflow_messages` / `worktrees`; doing so would bind a stable
reference that test patches against `workflow.X` could not affect.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple

from github.Issue import Issue

from .. import config
from ..agents import AgentResult
from ..config import RepoSpec
from ..github import GitHubClient, PinnedState


# Validating-side counterpart to in_review's `_TRANSIENT_PARK_REASONS`:
# park reasons whose underlying condition can resolve without any human
# comment. Without this, a transient validating failure would leave the
# issue parked forever -- `_resume_developer_on_human_reply` only fires on
# a new issue-thread comment, and the human action that unstuck the
# underlying condition (a flake clears, CI settles, the remote accepts
# the next push) typically does not include one.
#
#   `push_failed`     - non-fast-forward push; retried under --force-with-lease.
#   `agent_timeout`   - dev-fix agent timed out; let the next tick re-run the
#                       reviewer (which will spawn the dev again if changes
#                       are still requested).
#   `reviewer_timeout`- reviewer agent timed out; let the next tick re-run it.
#   `reviewer_failed` - reviewer agent silent-crashed (empty stdout +
#                       non-zero exit); same recovery as `reviewer_timeout`.
#
# Reasons that need human content (a question, a dirty worktree, a verdict
# the agent could not produce) stay parked until a comment arrives.
_VALIDATING_TRANSIENT_PARK_REASONS = frozenset(
    {"push_failed", "agent_timeout", "reviewer_timeout", "reviewer_failed"}
)


def _handle_dev_fix_result(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    wt: Path,
    result: AgentResult,
    before_sha: str,
) -> bool:
    """Post-agent handling for a dev fix during validating.

    Returns True if a fix was committed, pushed, and the loop should re-review
    on the next tick. Returns False if the run produced no fix (timeout,
    no-new-commit, dirty tree, or push failure); caller should write state and
    return.
    """
    from .. import workflow as _wf

    if result.timed_out:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent timed out after {config.AGENT_TIMEOUT}s, "
            "manual intervention needed.",
            reason="agent_timeout",
        )
        # Tag as transient: a stuck dev fix-loop (validating CHANGES_REQUESTED
        # or comment-driven resume) clears on the next tick when the validating
        # recovery branch lets the reviewer re-run; the in_review fix-loop
        # leaves it tagged but stays parked because in_review's transient set
        # does not include this reason.
        state.set("park_reason", "agent_timeout")
        # Persist the pre-agent SHA so the recovery branch can tell whether
        # the timeout actually produced a new commit. `_has_new_commits()`
        # would say yes for any normal PR worktree (the dev's earlier fixes
        # are ahead of `origin/<base>` even when this run did nothing), so
        # without this watermark the recovery would force-push a stale
        # local HEAD and bump the round on every tick.
        state.set("pre_dev_fix_sha", before_sha or "")
        return False

    after_sha = _wf._head_sha(wt)
    if after_sha == before_sha or not after_sha:
        # No new commit: dev asked a question or did nothing.
        _wf._on_question(gh, issue, state, result)
        return False

    # A new commit landed -- the session is alive and producing output, so
    # the silent-park streak must reset here. Otherwise a single later
    # empty resume would tip a healthy session past the fresh-session
    # threshold (`silent_park_count` is meant to count *consecutive*
    # silent parks, not lifetime silent parks). Covers the validating /
    # in_review fix paths whose success exit (`return True` below) bypasses
    # `_on_commits` / `_on_dirty_worktree`, which would otherwise be the
    # only resetters on this branch.
    state.set("silent_park_count", 0)

    dirty = _wf._worktree_dirty_files(wt)
    if dirty:
        _wf._on_dirty_worktree(gh, issue, state, result, dirty)
        return False

    branch = _wf._branch_name(issue.number)
    if not _wf._push_branch(spec, wt, branch):
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
            reason="push_failed",
        )
        # Tag as transient so a self-resolving condition (the next push
        # succeeds under --force-with-lease once the remote settles) can
        # silently recover the issue without needing a human comment.
        state.set("park_reason", "push_failed")
        return False

    return True


def _post_user_content_change_result(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    wt: Path,
    result: AgentResult,
    before_sha: str,
) -> str:
    """Post-resume handling for a user-content-change dev resume.

    Returns one of:

    * ``"ack"`` -- the dev produced no commit but explicitly signaled
      acknowledgement via the `ACK: ...` marker emitted by
      `_build_user_content_change_prompt`. The reply is posted on the
      issue as an FYI and the handler does NOT park `awaiting_human`.
      Caller stays on the current label.
    * ``"pushed"`` -- new commit landed and the push succeeded. Caller
      advances the label per its own rules (in_review bounces to
      validating; validating stays with `review_round++`).
    * ``"parked"`` -- timeout, dirty tree, push fail, silent crash
      (empty `last_message`), OR a no-commit response WITHOUT the
      `ACK:` marker (treated as a clarification question via
      `_on_question`). State already carries the park flags.

    The explicit `ACK:` marker is required because a generic non-empty
    no-commit response is often a clarification question, not an
    acknowledgement; swallowing it as an ack would post a misleading
    "existing work satisfies" comment AND continue the workflow with
    `awaiting_human=False`, stranding the real question.
    """
    from .. import workflow as _wf

    if result.timed_out:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent timed out after "
            f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
            reason="agent_timeout",
        )
        state.set("park_reason", "agent_timeout")
        state.set("pre_dev_fix_sha", before_sha or "")
        return "parked"

    after_sha = _wf._head_sha(wt)
    if not after_sha or after_sha == before_sha:
        ack_reason = _wf._drift_ack_reason(result.last_message or "")
        if ack_reason:
            quoted = "> " + ack_reason.replace("\n", "\n> ")
            _wf._post_issue_comment(
                gh, issue, state,
                ":speech_balloon: dev session reports the existing work "
                f"satisfies the edit:\n\n{quoted}",
            )
            # The session is alive and producing output, so the silent-
            # park streak must reset (mirrors `_handle_dev_fix_result`'s
            # reset on a successful commit) -- otherwise a future blip
            # would tip a healthy session past the fresh-session threshold.
            state.set("silent_park_count", 0)
            return "ack"
        # No commit and no explicit ACK marker. The reply may be a real
        # clarification question; falling through to `_on_question`
        # parks the issue awaiting human so a misleading "satisfies"
        # comment isn't posted over a real question. Empty messages
        # land in the same branch and surface as the silent-failure
        # park (`_resume_dev_with_text` uses the streak counter to
        # eventually drop a poisoned session id).
        _wf._on_question(gh, issue, state, result)
        return "parked"

    state.set("silent_park_count", 0)
    dirty = _wf._worktree_dirty_files(wt)
    if dirty:
        _wf._on_dirty_worktree(gh, issue, state, result, dirty)
        return "parked"

    branch = _wf._branch_name(issue.number)
    if not _wf._push_branch(spec, wt, branch):
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
            reason="push_failed",
        )
        state.set("park_reason", "push_failed")
        return "parked"

    return "pushed"


def _try_recover_validating_transient_park(
    spec: RepoSpec, issue: Issue, state: PinnedState
) -> bool:
    """Quietly attempt to clear a transient validating park.

    Returns True if the underlying condition has resolved (caller should
    clear the park flags and progress); False to stay parked. Must not
    spawn the agent or post issue/PR comments -- the caller owns the
    visible side of the recovery so a still-stuck tick produces no churn.

    The helper IS allowed to update review-round bookkeeping when a fix
    landed during recovery (e.g. an agent_timeout where the dev had
    actually committed before timing out, and we finish the push here).
    Callers should not mutate the round themselves; this is the only
    write path while the park flags are still set.
    """
    from .. import workflow as _wf

    park_reason = state.get("park_reason")
    if park_reason == "push_failed":
        wt = _wf._worktree_path(spec, issue.number)
        if not wt.exists():
            # Worktree was reaped; the dev's local commits are gone, so
            # there is nothing to push. A human has to intervene (relabel
            # back to implementing) -- that's the unblocking signal.
            return False
        if not _wf._push_branch(spec, wt, _wf._branch_name(issue.number)):
            return False
        # The dev's fix is now landed; bump the round so the cap reflects
        # the completed fix cycle.
        round_n = int(state.get("review_round") or 0)
        state.set("review_round", round_n + 1)
        return True
    if park_reason in ("reviewer_timeout", "reviewer_failed"):
        # Reviewer agent only reads the worktree; nothing to reconcile
        # locally. Clear flags so the next tick re-spawns the reviewer
        # with a fresh budget. `reviewer_failed` (silent crash with
        # empty stdout + non-zero exit) self-heals the same way as
        # `reviewer_timeout`: there is no dev-side state to reconcile,
        # and the next tick simply spawns a fresh reviewer.
        return True
    if park_reason == "agent_timeout":
        # The dev agent could have committed or left uncommitted edits
        # before the timeout killed it. Recovery cannot just clear flags
        # -- the next tick's reviewer would inspect the LOCAL worktree
        # and could approve a SHA that is not on the PR, seeding
        # `agent_approved_sha` to an unpushed commit and stalling
        # in_review. Reconcile the worktree explicitly here.
        wt = _wf._worktree_path(spec, issue.number)
        if not wt.exists():
            return False
        if _wf._worktree_dirty_files(wt):
            # The dev left edits that were never committed. We cannot
            # safely push, review, or auto-merge in this state; stay
            # parked until a human or a fresh comment-driven resume
            # sorts it out. A reviewer that ignored the dirty index
            # would vote on the committed head while the leftover edits
            # are silently dropped on the next push.
            return False
        # The pre-agent SHA was persisted when the timeout park ran.
        # Compare against the current worktree HEAD instead of
        # `_has_new_commits()`, which only checks against
        # `origin/<base>` and would always say "yes" for a PR worktree
        # whose earlier fixes already shipped.
        pre_sha = state.get("pre_dev_fix_sha")
        if not isinstance(pre_sha, str):
            # Defensive: the timeout-tagging path always persists this,
            # so a missing watermark means foreign state we cannot
            # reason about. Stay parked rather than risk force-pushing
            # an out-of-date HEAD over the remote.
            return False
        now_sha = _wf._head_sha(wt)
        if not now_sha or now_sha == pre_sha:
            # The timeout produced no new commit. Clear flags but do
            # not bump the round or push -- nothing landed.
            state.set("pre_dev_fix_sha", None)
            return True
        # The dev committed before timing out. Finish what it started
        # by pushing the new SHA; on success the fix is now landed and
        # we bump the round just like the push_failed branch.
        if not _wf._push_branch(spec, wt, _wf._branch_name(issue.number)):
            return False
        state.set("pre_dev_fix_sha", None)
        round_n = int(state.get("review_round") or 0)
        state.set("review_round", round_n + 1)
        return True
    return False


def _seed_watermark_past_self(
    issue_thread_comments: list,
    pr_conversation_comments: list,
    orchestrator_ids: set[int],
    pickup_comment_id: Optional[int],
    consumed_through: Optional[int] = None,
) -> Optional[int]:
    """Seed the in_review handoff watermark.

    Walk comments oldest-to-newest across both surfaces (issue thread and
    PR conversation share the IssueComment id space, so a single watermark
    covers both). The pickup comment is the boundary: everything before
    `pickup_comment_id` is pre-pickup chatter the dev agent already saw at
    spawn, so it can be advanced past. From the pickup forward, advance
    through the contiguous run of orchestrator-authored comments AND
    through any ISSUE-THREAD comment with id <= `consumed_through` (already
    fed to the dev agent via a prior `_resume_developer_on_human_reply`
    call during implementing/validating), stopping at the first
    not-yet-consumed non-orchestrator comment. This preserves human
    feedback posted during validating that the dev has not yet seen while
    NOT replaying feedback the dev has already consumed.

    `consumed_through` is intentionally NOT applied to PR-conversation
    comments. `last_action_comment_id` only records issue-thread ids fed
    via `_resume_developer_on_human_reply` (validating/implementing watch
    the issue thread only); a PR-conversation comment whose id happens to
    be <= a later-consumed issue-thread reply has NOT been seen by the dev
    and must surface on the next in_review tick. Folding both surfaces
    under one `c.id <= consumed_through` check would let AUTO_MERGE land
    the PR over unread PR-conversation feedback.

    Identification of orchestrator-authored content is by exact comment id
    (recorded when the orchestrator posted the comment) rather than author
    login. The login-based check would also drop comments authored by a
    human reviewer who shares the PAT's GitHub account -- a common
    deployment shape -- causing real review feedback to be auto-merged over.

    Returns None when the pickup id is unknown (legacy state from a deploy
    that pre-dates pickup-id tracking, or a manually-relabeled issue) or
    when the surface has no orchestrator-authored content. The caller then
    defaults the watermark to 0 so the in_review legacy migration cannot
    advance past historical content; the orchestrator_comment_ids id-set
    filter in `_handle_in_review` drops recorded bot comments at scan time.
    """
    if pickup_comment_id is None:
        # Legacy state without a pickup anchor: refuse to advance. We
        # cannot tell pre-pickup chatter (safe to skip) from human feedback
        # posted during implementing/validating (must preserve), and
        # dropping a human comment is the unsafe direction.
        return None
    # Tag each comment with its surface so the walk below can apply
    # `consumed_through` to the issue thread only.
    sorted_pairs: list[Tuple[Any, bool]] = sorted(
        [(c, True) for c in issue_thread_comments]
        + [(c, False) for c in pr_conversation_comments],
        key=lambda p: p[0].id,
    )
    if not any(c.id in orchestrator_ids for c, _ in sorted_pairs):
        return None
    watermark: Optional[int] = None
    seen_self = False
    for c, is_issue_thread in sorted_pairs:
        is_self = c.id in orchestrator_ids
        already_consumed = (
            is_issue_thread
            and consumed_through is not None
            and c.id <= consumed_through
        )
        if is_self:
            watermark = c.id
            seen_self = True
        elif not seen_self and c.id < pickup_comment_id:
            # Pre-pickup chatter -- already in the dev agent's spawn context.
            watermark = c.id
        elif already_consumed:
            # Fed to the dev via a prior implementing/validating resume.
            # Replaying it as fresh PR feedback would re-spawn the dev on
            # input it has already handled.
            watermark = c.id
        else:
            # Post-pickup human comment that has NOT been consumed yet.
            # Stop and preserve for the next in_review tick.
            break
    return watermark


def _latest_pr_comment_ids(
    gh: GitHubClient, issue: Issue, pr, state: PinnedState
) -> Tuple[Optional[int], Optional[int]]:
    """Return (issue-comment watermark, review-comment watermark) seeded only
    past leading orchestrator-authored comments on the issue thread + PR.

    The second value is always None: the orchestrator never posts inline PR
    review comments, so there is no leading self-run to advance past on
    that surface, and `orchestrator_comment_ids` records IDs in the
    IssueComment namespace only -- feeding it to `_seed_watermark_past_self`
    against the PullRequestComment namespace would falsely treat a human
    inline comment whose numeric id collides with a recorded bot id as
    self-authored, advancing the watermark past the human's feedback. The
    `_handle_validating` caller defaults the inline-review watermark to 0
    when this returns None so the in_review legacy migration cannot then
    advance past human inline feedback either.
    """
    from .. import workflow as _wf

    orchestrator_ids = _wf._orchestrator_ids(state)
    pickup_id_raw = state.get("pickup_comment_id")
    pickup_id = pickup_id_raw if isinstance(pickup_id_raw, int) else None
    # `last_action_comment_id` doubles as a "consumed through" marker:
    # both park comments and post-resume bumps land here, so any issue
    # comment with id <= this value has either been posted by the
    # orchestrator (filtered by `orchestrator_comment_ids`) or already
    # been fed to the dev session (must not replay).
    consumed_raw = state.get("last_action_comment_id")
    consumed_through = (
        consumed_raw if isinstance(consumed_raw, int) else None
    )
    # Keep the surfaces separate -- `consumed_through` only applies to the
    # issue thread (the surface `_resume_developer_on_human_reply` watches
    # during implementing/validating). Folding both into one list and
    # applying `c.id <= consumed_through` uniformly would silently advance
    # the watermark past unread PR-conversation feedback whose id happens
    # to be lower than a later-consumed issue-thread reply, letting
    # AUTO_MERGE land the PR over the human's PR comment.
    issue_thread = list(gh.comments_after(issue, None))
    pr_conversation = list(gh.pr_conversation_comments_after(pr, None))
    return (
        _seed_watermark_past_self(
            issue_thread, pr_conversation,
            orchestrator_ids, pickup_id,
            consumed_through=consumed_through,
        ),
        None,
    )


def _handle_validating(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    # User-content drift: a human edited the issue title/body while the
    # reviewer was running. Re-decomposing now would discard the dev's
    # already-pushed work, so notify the human, resume the dev session on
    # its locked backend with the new body, and on a successful pushed fix
    # stay in `validating` so the reviewer re-runs on the new diff next
    # tick. On a failed resume (timeout, dirty, no commit), the standard
    # park flags land via `_handle_dev_fix_result`.
    #
    # Exception: when the issue is parked with a reviewer-side park reason
    # (`reviewer_timeout` / `reviewer_failed`), defer to the awaiting-human
    # branch below. A human "retry" comment on a reviewer-side park must
    # re-spawn the REVIEWER, not the dev: the failure produced no review
    # output for the dev to act on, and the reviewer naturally re-reads
    # the updated `issue.body` + comments via `_build_review_prompt` when
    # it runs. We still persist the new baseline here so the next tick's
    # drift check sees a stable comparison point (otherwise the drift
    # would loop on every subsequent tick).
    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        state.set("user_content_hash", new_hash)
        reviewer_side_park = (
            state.get("awaiting_human")
            and state.get("park_reason")
            in ("reviewer_timeout", "reviewer_failed")
        )
        if not reviewer_side_park:
            _wf._post_issue_comment(
                gh, issue, state,
                ":pencil2: issue body changed; resuming dev session.",
            )
            # Mark the full issue thread as consumed: the dev sees it via
            # `_recent_comments_text` in the resume prompt, so the eventual
            # handoff to in_review must not replay those comments as fresh
            # feedback. Mirrors `_resume_developer_on_human_reply`'s
            # pre-spawn bump.
            _wf._mark_drift_comments_consumed(gh, issue, state)
            wt = _wf._worktree_path(spec, issue.number)
            if not wt.exists():
                wt = _wf._ensure_worktree(spec, issue.number)
            before_sha = _wf._head_sha(wt)
            followup = _wf._build_user_content_change_prompt(
                issue, _wf._recent_comments_text(issue),
            )
            wt, result = _wf._resume_dev_with_text(
                gh, spec, issue, state, followup,
            )
            state.set("last_agent_action_at", _wf._now_iso())
            # Custom result handler: a no-commit-with-message reply is the
            # dev confirming the existing work already satisfies the edit,
            # and the resume prompt explicitly invites that response.
            # `_handle_dev_fix_result` would park on it via `_on_question`;
            # use the user-content-specific helper so a harmless clarification
            # does not stall the issue.
            outcome = _post_user_content_change_result(
                gh, spec, issue, state, wt, result, before_sha,
            )
            if outcome == "pushed":
                round_n = int(state.get("review_round") or 0)
                state.set("review_round", round_n + 1)
            gh.write_pinned_state(issue, state)
            return
        # reviewer-side park: fall through to the awaiting_human branch
        # below, which will consume the human's "retry" comment, clear
        # the park flags, and re-spawn the reviewer.

    # Awaiting-human path: human replied after a park; resume the developer
    # codex with their feedback. Identical mechanic to implementing's resume,
    # but on success we stay in validating and bump the round so the reviewer
    # runs again on the next tick.
    if state.get("awaiting_human"):
        # Transient-park recovery: when the original park reason is something
        # that can resolve without a human comment (a push race that the
        # next --force-with-lease push will land, or an agent timeout that
        # the next tick can simply rerun past), re-attempt silently. This
        # mirrors the in_review recovery branch -- without it, the issue
        # would sit forever, because `_resume_developer_on_human_reply`
        # only fires on new issue-thread comments and the human action
        # that unstuck the underlying condition typically does not include
        # one.
        last_action_id = state.get("last_action_comment_id")
        new_comments = gh.comments_after(issue, last_action_id)
        park_reason = state.get("park_reason")
        if (
            not new_comments
            and park_reason in _VALIDATING_TRANSIENT_PARK_REASONS
        ):
            if not _try_recover_validating_transient_park(
                spec, issue, state
            ):
                return  # still stuck, do not re-post the park comment
            # Conditions resolved: clear the park flags. The recovery
            # helper has already bumped review_round when a fix landed
            # (push_failed, or agent_timeout that finished its push), so
            # we only handle the flag clear here. The handoff watermark
            # `last_action_comment_id` was already advanced by the
            # original `_park_awaiting_human` call, so the in_review
            # handler will not re-feed any already-consumed comments.
            state.set("awaiting_human", False)
            state.set("park_reason", None)
            gh.write_pinned_state(issue, state)
            return
        if (
            new_comments
            and park_reason in ("reviewer_timeout", "reviewer_failed")
        ):
            # The park was reviewer-side (timeout or silent crash); a
            # human "Retry" / "Continue" nudge should re-spawn the
            # REVIEWER, not the dev. The dev session has nothing to act
            # on -- the failure produced no review output -- and waking
            # it just yields a "nothing to do" question that re-parks
            # the issue. Advance the watermark past the consumed
            # comments, clear the park flags, and fall through to the
            # reviewer-spawn block below.
            consumed_max = max(c.id for c in new_comments)
            state.set("last_action_comment_id", consumed_max)
            state.set("awaiting_human", False)
            state.set("park_reason", None)
        else:
            wt = _wf._worktree_path(spec, issue.number)
            if not wt.exists():
                wt = _wf._ensure_worktree(spec, issue.number)
            before_sha = _wf._head_sha(wt)
            resumed = _wf._resume_developer_on_human_reply(gh, spec, issue, state)
            if resumed is None:
                return
            wt, result = resumed
            state.set("last_agent_action_at", _wf._now_iso())
            if not _handle_dev_fix_result(
                gh, spec, issue, state, wt, result, before_sha
            ):
                gh.write_pinned_state(issue, state)
                return
            round_n = int(state.get("review_round") or 0)
            state.set("review_round", round_n + 1)
            gh.write_pinned_state(issue, state)
            return

    round_n = int(state.get("review_round") or 0)
    if round_n >= config.MAX_REVIEW_ROUNDS:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} review still has comments after "
            f"{round_n} round(s); manual intervention needed.",
            reason="review_cap",
        )
        gh.write_pinned_state(issue, state)
        return

    wt = _wf._ensure_worktree(spec, issue.number)
    # The reviewer reads the local worktree's HEAD; remember which commit
    # that is so the in_review handoff can persist the SHA the agent
    # actually inspected. Setting `agent_approved_sha = pr.head.sha`
    # instead would mark the REMOTE head at handoff time as agent-approved,
    # which lets AUTO_MERGE land an unreviewed commit if the branch was
    # force-pushed or otherwise updated between the review and the handoff.
    reviewed_sha = _wf._head_sha(wt)
    _, dev_backend_for_prompt, _, _ = _wf._read_dev_session(state)
    review_prompt = _wf._build_review_prompt(
        spec, issue, _wf._recent_comments_text(issue), dev_backend_for_prompt,
    )
    # Persist the full configured spec BEFORE the spawn so a reviewer
    # backend hiccup that yields no session id still leaves a durable
    # role-identity record. The trace reflects the reviewer's CLI args
    # and a config flip mid-flight cannot retroactively rewrite which
    # spec ran each round. The reviewer is spawned fresh each round
    # (no resume), so always overwriting the field with the current
    # config spec is the right behavior here.
    state.set("review_agent", config.REVIEW_AGENT_SPEC)
    review = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="reviewer",
        stage="validating",
        backend=config.REVIEW_AGENT,
        prompt=review_prompt,
        cwd=wt,
        timeout=config.REVIEW_TIMEOUT,
        extra_args=config.REVIEW_AGENT_ARGS,
        review_round=round_n,
        retry_count=state.get("retry_count"),
    )
    if review.session_id:
        state.set("last_review_session_id", review.session_id)
    state.set("last_review_at", _wf._now_iso())

    if review.timed_out:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} reviewer timed out after "
            f"{config.REVIEW_TIMEOUT}s; manual intervention needed.",
            reason="reviewer_timeout",
        )
        # Tag as transient so the next tick re-spawns the reviewer instead
        # of waiting for a human comment that the timeout itself does not
        # produce.
        state.set("park_reason", "reviewer_timeout")
        gh.write_pinned_state(issue, state)
        return

    verdict, body = _wf._parse_review_verdict(review.last_message)
    gh.emit_event(
        "review_verdict",
        issue_number=issue.number,
        stage="validating",
        verdict=verdict,
        review_round=round_n,
        pr_number=int(pr_number) if pr_number is not None else None,
        session_id=review.session_id,
    )

    if verdict == "approved":
        if pr_number is not None:
            try:
                _wf._post_pr_comment(
                    gh, int(pr_number), state,
                    f":white_check_mark: {config.REVIEW_AGENT} review approved.",
                )
            except Exception:
                _wf.log.exception(
                    "issue=#%s could not post approval to PR #%s",
                    issue.number, pr_number,
                )

        # Squash before seeding the in_review handoff. If the squash or
        # force-push fails we park awaiting_human and STAY in `validating`
        # (no relabel), so the original commits remain on the branch and a
        # human can adjudicate. On success the new local HEAD becomes the
        # SHA AUTO_MERGE will gate on; the existing
        # `agent_approved_sha == pr.head.sha` invariant then keeps holding
        # because the remote also points at the new SHA after the
        # force-push, and `_latest_review_states_for_head` naturally
        # invalidates any stale GitHub review (its `commit_id` no longer
        # matches the new head), leaving the agent's `agent_approved_sha`
        # as the only thing keeping AUTO_MERGE viable -- which is exactly
        # the point.
        new_head_sha = reviewed_sha
        squashed_count = 0
        if config.SQUASH_ON_APPROVAL:
            success, sha_after, n_squashed, err = _wf._squash_and_force_push(
                spec, wt, _wf._branch_name(issue.number), issue,
            )
            if not success:
                _wf._park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} squash-on-approval failed "
                    f"({err}); the original commits are still on the "
                    "branch and the PR was not relabeled. Manual "
                    "intervention needed (squash + force-push by hand, "
                    "or set `SQUASH_ON_APPROVAL=off` and re-run the "
                    "reviewer).",
                    reason="squash_failed",
                )
                gh.write_pinned_state(issue, state)
                return
            if sha_after:
                new_head_sha = sha_after
            squashed_count = n_squashed

        if pr_number is not None:
            # Snapshot what the reviewer agent approved and seed the
            # in_review comment watermark. Without these, `_handle_in_review`
            # would (a) refuse to auto-merge -- the agent posts an issue
            # comment, not a real PR review, so pr_is_approved alone is
            # always False for the agent flow -- and (b) replay the
            # orchestrator's own automated comments ("picking this up",
            # "PR opened", the approval just posted) as fresh PR feedback
            # once the debounce expires.
            try:
                pr = gh.get_pr(int(pr_number))
            except Exception as e:
                # Recoverable: AUTO_MERGE will simply not fire for this
                # issue, and the in_review handler will fall back to its
                # legacy `last_action_comment_id` watermark. Surface the
                # failure but skip the traceback -- it adds no signal.
                _wf.log.warning(
                    "issue=#%s could not snapshot PR #%s for in_review "
                    "handoff: %s", issue.number, pr_number, e,
                )
            else:
                # Post the squash PR comment BEFORE seeding watermarks so
                # the seed walks past it (its id lands in
                # `orchestrator_comment_ids` via `_post_pr_comment`).
                # Without that ordering, the next in_review tick treats
                # the squash comment as fresh PR feedback once the
                # debounce expires and resumes the dev session over an
                # informational orchestrator post.
                if squashed_count > 1:
                    try:
                        _wf._post_pr_comment(
                            gh, int(pr_number), state,
                            f":package: squashed {squashed_count} commits "
                            "to 1 after approval",
                        )
                    except Exception:
                        _wf.log.exception(
                            "issue=#%s could not post squash notice to "
                            "PR #%s", issue.number, pr_number,
                        )
                # Persist the local SHA the reviewer (or the squash)
                # produced, not the current remote head. The auto-merge
                # gate's existing `agent_approved_sha == head_sha` check
                # then naturally rejects the branch-update race: if the
                # remote moves past this SHA, agent_approved_sha won't
                # match and AUTO_MERGE waits for a fresh review round.
                if new_head_sha:
                    state.set("agent_approved_sha", new_head_sha)
                issue_wm, review_wm = _latest_pr_comment_ids(
                    gh, issue, pr, state
                )
                # Ratchet: a previous in_review tick may have already
                # advanced these watermarks past PR feedback the dev has
                # since fixed. _seed_watermark_past_self stops at the first
                # post-pickup human comment, so without max() that consumed
                # comment would replay as "new" on the next in_review tick.
                prev_issue_wm = state.get("pr_last_comment_id")
                if isinstance(prev_issue_wm, int):
                    issue_wm = (
                        prev_issue_wm if issue_wm is None
                        else max(issue_wm, prev_issue_wm)
                    )
                # Default to 0 ("scan all from the beginning") when the
                # seed-past-self logic returned None and no prior watermark
                # exists. That happens for legacy state without a recorded
                # pickup id; setting 0 stops the in_review legacy migration
                # from then advancing past historical content (including
                # human feedback posted during implementing/validating)
                # while still letting `orchestrator_comment_ids` filter
                # recorded bot comments out of the next tick's scan.
                if issue_wm is None:
                    issue_wm = 0
                state.set("pr_last_comment_id", issue_wm)
                # Inline review comments and review summaries live in
                # namespaces the orchestrator never posts on, so the
                # seed-past-self logic always returns None for those
                # surfaces. Default each to 0 ("scan all from beginning")
                # so the in_review legacy migration sees them as already
                # seeded and does NOT advance past human feedback the
                # human submitted on those surfaces during validate. Ratchet
                # past anything a prior in_review tick already consumed.
                prev_review_wm = state.get("pr_last_review_comment_id")
                if isinstance(prev_review_wm, int):
                    review_wm = (
                        prev_review_wm if review_wm is None
                        else max(review_wm, prev_review_wm)
                    )
                if review_wm is None:
                    review_wm = 0
                state.set("pr_last_review_comment_id", review_wm)
                prev_summary_wm = state.get("pr_last_review_summary_id")
                summary_wm = (
                    prev_summary_wm
                    if isinstance(prev_summary_wm, int)
                    else 0
                )
                state.set("pr_last_review_summary_id", summary_wm)
        gh.set_workflow_label(issue, "in_review")
        gh.write_pinned_state(issue, state)
        return

    if verdict == "unknown":
        raw = (review.last_message or "").strip() or "(reviewer produced no final message)"
        quoted = "> " + raw.replace("\n", "\n> ")
        # Surface stderr only on the silent-review path (empty last_message).
        # If the reviewer DID emit text but it just lacked a VERDICT line,
        # the human is reading real model output and stderr noise would
        # only distract.
        silent_crash = (
            not (review.last_message or "").strip() and review.exit_code != 0
        )
        diag = (
            _wf._format_stderr_diagnostics(review, "Reviewer")
            if not (review.last_message or "").strip()
            else ""
        )
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} reviewer did not emit a VERDICT line; "
            f"manual adjudication needed.\n\n_Last reviewer message:_\n\n"
            f"{quoted}{diag}",
            reason="reviewer_failed" if silent_crash else "reviewer_no_verdict",
        )
        if silent_crash:
            # Tag as transient so the next tick re-spawns the reviewer
            # instead of waking the dev on a human "Retry" comment.
            # Mirrors `reviewer_timeout`: a crash with empty stdout +
            # non-zero exit (codex-side error, network blip) leaves no
            # output the dev could act on, and `_resume_developer_on_human_reply`
            # would otherwise hand the wrong agent a do-nothing prompt.
            state.set("park_reason", "reviewer_failed")
        _wf.log.warning(
            "issue=#%s reviewer emitted no VERDICT; exit_code=%d "
            "timed_out=%s stderr_tail=%r",
            issue.number, review.exit_code, review.timed_out,
            _wf._stderr_log_tail(review),
        )
        gh.write_pinned_state(issue, state)
        return

    # CHANGES_REQUESTED -- post the feedback on the PR, then resume the dev.
    feedback = body.strip() or (review.last_message or "").strip()
    if pr_number is not None:
        try:
            _wf._post_pr_comment(
                gh, int(pr_number), state,
                f":eyes: {config.REVIEW_AGENT} review (round {round_n + 1}/"
                f"{config.MAX_REVIEW_ROUNDS}) requested changes:\n\n{feedback}",
            )
        except Exception:
            _wf.log.exception(
                "issue=#%s could not post review to PR #%s",
                issue.number, pr_number,
            )

    fix_prompt = _wf._build_fix_prompt(feedback)
    before_sha = _wf._head_sha(wt)
    _, dev_backend, dev_args, dev_sid = _wf._read_dev_session(state)
    dev_result = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="developer",
        stage="validating",
        backend=dev_backend,
        prompt=fix_prompt,
        cwd=wt,
        resume_session_id=dev_sid,
        extra_args=dev_args,
        review_round=round_n,
        retry_count=state.get("retry_count"),
    )
    state.set("last_agent_action_at", _wf._now_iso())

    if not _handle_dev_fix_result(
        gh, spec, issue, state, wt, dev_result, before_sha
    ):
        gh.write_pinned_state(issue, state)
        return

    state.set("review_round", round_n + 1)
    gh.write_pinned_state(issue, state)
