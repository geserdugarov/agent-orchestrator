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

import re
from pathlib import Path
from typing import Any, Optional, Tuple

from github.Issue import Issue

from .. import config
from ..agents import AgentResult
from ..config import RepoSpec
from ..state_machine import WorkflowLabel
from ..github import GitHubClient, PinnedState


# Operator escape hatch for `park_reason=review_cap`. Resets the review
# loop without losing the PR/worktree (see `_handle_validating`). The
# command lives in the issue thread because the cap-park message lands
# there, and is anchored to start-of-line so prose like "we should run
# `/orchestrator add-review-rounds 2`" cannot fire it accidentally.
_ADD_REVIEW_ROUNDS_RE = re.compile(
    r"^\s*/orchestrator\s+add-review-rounds\s+(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_add_review_rounds(
    comments: list,
) -> Optional[Tuple[int, Optional[str]]]:
    """Find the latest `/orchestrator add-review-rounds N` command across
    `comments`.

    Returns ``(n, None)`` for a valid positive `N`; ``(n, reason)`` when
    the latest match has an invalid argument (caller posts `reason` and
    stays parked); ``None`` when no comment carries the command. Walks
    newest-first so a corrected command supersedes a stale one posted
    earlier in the same batch.
    """
    for c in reversed(comments):
        body = c.body or ""
        m = _ADD_REVIEW_ROUNDS_RE.search(body)
        if not m:
            continue
        n = int(m.group(1))
        if n <= 0:
            return (n, f"expected a positive integer (got `{n}`)")
        return (n, None)
    return None


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
    after_sha: Optional[str] = None,
) -> bool:
    """Post-agent handling for a dev fix during validating.

    Returns True if a fix was committed, pushed, and the caller should
    advance the label (validating routes the issue back to `validating`
    on True so the reviewer re-runs against the new head; any stale
    approval state must be reset by the caller before relabeling).
    Returns False if the run produced no fix (timeout, no-new-commit,
    dirty tree, or push failure); caller should write state and return.

    `after_sha`, when provided, is the post-agent HEAD the caller already
    read (e.g. the fixing handler's ACK fast path); passing it avoids a
    redundant `_head_sha` call. When None it is read here.
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

    if after_sha is None:
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

    branch = _wf._resolve_branch_name(state, spec, issue.number)
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
      Caller decides what to do with the label: validating stays put
      (the reviewer reruns on the current head); in_review bounces
      back to `validating` (the prior reviewer approval was for the
      old requirements, so the in_review HITL ready-ping must wait
      for a re-approval) WITHOUT spawning `documenting` -- no commit
      landed for the docs pass to react to.
    * ``"pushed"`` -- new commit landed and the push succeeded.
      Validating stays on `validating` (and bumps `review_round`) so
      the reviewer re-evaluates the new head; in_review also hands
      straight back to `validating`. Docs are not run on this exit --
      the single docs pass is deferred to the final-docs handoff after
      reviewer approval. Any stale approval state must be reset by
      the caller before relabeling.
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

    branch = _wf._resolve_branch_name(state, spec, issue.number)
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
) -> str:
    """Quietly attempt to clear a transient validating park.

    Returns one of:
      * ``"stuck"`` -- the underlying condition has not resolved; caller
        leaves the park flags in place and returns silently.
      * ``"cleared"`` -- the park can be cleared, but nothing new
        landed on the PR (reviewer-only crash, or a dev-timeout that
        had not actually produced a commit). Caller clears the flags
        and stays on `validating` so the reviewer reruns.
      * ``"pushed"`` -- a dev fix was finished off during recovery
        (a deferred push of `push_failed`, or the trailing push of an
        `agent_timeout` that had committed before being killed).
        Caller clears the flags, resets stale approval state, and
        stays on `validating` so the reviewer re-evaluates the new
        head.

    Must not spawn the agent or post issue/PR comments -- the caller owns
    the visible side of the recovery so a still-stuck tick produces no
    churn.

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
            return "stuck"
        if not _wf._push_branch(spec, wt, _wf._resolve_branch_name(state, spec, issue.number)):
            return "stuck"
        # The dev's fix is now landed; bump the round so the cap reflects
        # the completed fix cycle.
        round_n = int(state.get("review_round") or 0)
        state.set("review_round", round_n + 1)
        return "pushed"
    if park_reason in ("reviewer_timeout", "reviewer_failed"):
        # Reviewer agent only reads the worktree; nothing to reconcile
        # locally. Clear flags so the next tick re-spawns the reviewer
        # with a fresh budget. `reviewer_failed` (silent crash with
        # empty stdout + non-zero exit) self-heals the same way as
        # `reviewer_timeout`: there is no dev-side state to reconcile,
        # and the next tick simply spawns a fresh reviewer.
        return "cleared"
    if park_reason == "agent_timeout":
        # The dev agent could have committed or left uncommitted edits
        # before the timeout killed it. Recovery cannot just clear flags
        # -- the next tick's reviewer would inspect the LOCAL worktree
        # and could approve a SHA that is not on the PR. Reconcile the
        # worktree explicitly here.
        wt = _wf._worktree_path(spec, issue.number)
        if not wt.exists():
            return "stuck"
        if _wf._worktree_dirty_files(wt):
            # The dev left edits that were never committed. We cannot
            # safely push, review, or advertise a ready-for-merge state
            # here; stay parked until a human or a fresh comment-driven
            # resume sorts it out. A reviewer that ignored the dirty
            # index would vote on the committed head while the leftover
            # edits are silently dropped on the next push.
            return "stuck"
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
            return "stuck"
        now_sha = _wf._head_sha(wt)
        if not now_sha or now_sha == pre_sha:
            # The timeout produced no new commit. Clear flags but do
            # not bump the round or push -- nothing landed.
            state.set("pre_dev_fix_sha", None)
            return "cleared"
        # The dev committed before timing out. Finish what it started
        # by pushing the new SHA; on success the fix is now landed and
        # we bump the round just like the push_failed branch.
        if not _wf._push_branch(spec, wt, _wf._resolve_branch_name(state, spec, issue.number)):
            return "stuck"
        state.set("pre_dev_fix_sha", None)
        round_n = int(state.get("review_round") or 0)
        state.set("review_round", round_n + 1)
        return "pushed"
    return "stuck"


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
    under one `c.id <= consumed_through` check would let the in_review
    HITL ready-ping advertise the PR as ready for human merge over
    unread PR-conversation feedback.

    Identification of orchestrator-authored content is by exact comment id
    (recorded when the orchestrator posted the comment) OR by the hidden
    body marker `_ORCH_COMMENT_MARKER` -- mirroring the in_review feedback
    filter. The id-only check would mis-treat a bot comment whose id was
    evicted from the bounded `orchestrator_comment_ids` cap (or never
    persisted due to a state-write race) as a human comment, stopping the
    walker early and stranding the watermark at a low value: the next
    in_review tick would then re-scan the same orchestrator content on
    every poll (the in_review filter still drops it via the marker, but
    the walker should not amplify that cost), and once a real human
    comment lands ABOVE the orchestrator backlog the seed walker would
    keep yielding a stale watermark indefinitely. The login-based check
    would also drop comments authored by a human reviewer who shares the
    PAT's GitHub account -- a common deployment shape -- causing real
    review feedback to be silently dropped and the PR to be pinged ready
    for human merge over it.

    Returns None when the pickup id is unknown (legacy state from a deploy
    that pre-dates pickup-id tracking, or a manually-relabeled issue) or
    when the surface has no orchestrator-authored content. The caller then
    defaults the watermark to 0 so the in_review legacy migration cannot
    advance past historical content; the orchestrator_comment_ids id-set
    filter in `_handle_in_review` drops recorded bot comments at scan time.
    """
    from .. import workflow as _wf

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

    def _is_self(c) -> bool:
        return (
            c.id in orchestrator_ids
            or _wf._ORCH_COMMENT_MARKER in (getattr(c, "body", None) or "")
        )

    if not any(_is_self(c) for c, _ in sorted_pairs):
        return None
    watermark: Optional[int] = None
    seen_self = False
    for c, is_issue_thread in sorted_pairs:
        is_self = _is_self(c)
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
    # to be lower than a later-consumed issue-thread reply, letting the
    # in_review HITL ready-ping advertise the PR as ready for human
    # merge over the human's PR comment.
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


_VERIFY_STATUS_TO_REASON = {
    "failed": "verify_failed",
    "timeout": "verify_timeout",
    "dirty": "verify_dirty",
    "head_changed": "verify_head_changed",
}


def _park_verify_failure(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    verify,
) -> None:
    """Park `validating` on a local-verify failure.

    The park comment names the failing command, its exit code (or
    timeout), and a redacted / truncated tail of the captured output so
    the operator can triage without pulling the orchestrator's logs.
    `park_reason` is set to a stable token (`verify_failed`,
    `verify_timeout`, or `verify_dirty`) so dashboards and future
    transient-recovery logic can branch on the failure mode.
    """
    from .. import workflow as _wf

    reason = _VERIFY_STATUS_TO_REASON.get(verify.status, "verify_failed")
    if verify.status == "timeout":
        detail = (
            f"`{verify.command}` timed out after "
            f"{config.VERIFY_TIMEOUT}s"
        )
    elif verify.status == "dirty":
        files = ", ".join(f"`{p}`" for p in verify.dirty_files[:10])
        if len(verify.dirty_files) > 10:
            files += f", … (+{len(verify.dirty_files) - 10} more)"
        detail = (
            f"`{verify.command}` left the worktree dirty: {files}"
        )
    elif verify.status == "head_changed":
        # The verify command produced a new commit (or otherwise moved
        # HEAD) on its own. Surface both SHAs so the operator can
        # `git show` the commit and decide whether to keep it
        # (re-spawn the reviewer on the new HEAD) or revert it before
        # re-trying. Show short SHAs for legibility.
        before = (verify.head_before or "")[:12] or "(no HEAD)"
        after = (verify.head_after or "")[:12] or "(no HEAD)"
        detail = (
            f"`{verify.command}` moved HEAD ({before} -> {after}); "
            "verify commands must not commit"
        )
    else:
        detail = (
            f"`{verify.command}` exited with code "
            f"{verify.exit_code if verify.exit_code is not None else '?'}"
        )

    message = (
        f"{config.HITL_MENTIONS} local verification failed; PR not handed "
        f"off to in_review. {detail}."
    )
    # `verify.output` is already redacted-then-truncated by the runner;
    # re-redacting here would be a no-op for any match `_redact_secrets`
    # already collapsed to `***`, AND would not catch a partial secret
    # that straddled the truncation cut -- the only safe way to handle
    # that case is the redact-before-truncate pass inside the runner.
    output = verify.output or ""
    if output.strip():
        quoted = "> " + output.rstrip().replace("\n", "\n> ")
        message += f"\n\n_Verify output (tail):_\n\n{quoted}"

    _wf._park_awaiting_human(gh, issue, state, message, reason=reason)
    state.set("park_reason", reason)


def _handle_validating(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    # External merge short-circuit: a human merged the PR while the
    # reviewer was queued. Finalize to `done` here rather than running
    # the reviewer agent against a branch that already landed; the
    # in_review / fixing handlers have an equivalent terminal check.
    if _wf._finalize_if_pr_merged(gh, spec, issue, state):
        return

    # Closed-issue counterpart: the closed-`validating` sweep yields
    # issues a human closed without a merged PR (the operator rejected
    # the change mid-review, or the PR was closed-without-merge). Flip
    # to `rejected` so the reviewer agent does not spawn against a
    # closed issue and the PR is not relabeled back to `in_review`.
    if _wf._finalize_if_issue_closed(gh, spec, issue, state):
        return

    # User-content drift: a human edited the issue title/body while the
    # reviewer was running. Re-decomposing now would discard the dev's
    # already-pushed work, so notify the human, resume the dev session on
    # its locked backend with the new body, and on a successful pushed fix
    # bump `review_round` while staying on `validating` (no relabel
    # emitted) so the reviewer re-evaluates the updated body + new diff
    # on the next tick. An ACK reply (no commit) keeps the issue on
    # `validating`. On a failed resume (timeout, dirty, no commit), the
    # standard park flags land via `_handle_dev_fix_result`.
    #
    # Exception: when the issue is parked with a reviewer-side park reason
    # (`reviewer_timeout` / `reviewer_failed`) OR on the review-round cap
    # (`review_cap`), defer to the awaiting-human branch below. A human
    # "retry" comment on a reviewer-side park must re-spawn the REVIEWER,
    # not the dev: the failure produced no review output for the dev to
    # act on, and the reviewer naturally re-reads the updated `issue.body`
    # + comments via `_build_review_prompt` when it runs. For `review_cap`,
    # the cap has consumed every round, so resuming the dev would re-park
    # on the cap next tick (the original bug); the operator's
    # `/orchestrator add-review-rounds` command lives in the awaiting-human
    # branch, and the operator's command comment itself is a non-orchestrator
    # comment that bumps the user-content hash, so without this bypass the
    # drift block fires first and the command never gets parsed. We still
    # persist the new baseline here so the next tick's drift check sees a
    # stable comparison point (otherwise the drift would loop on every
    # subsequent tick).
    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        state.set("user_content_hash", new_hash)
        defer_to_awaiting_human = (
            state.get("awaiting_human")
            and state.get("park_reason")
            in ("reviewer_timeout", "reviewer_failed", "review_cap")
        )
        if not defer_to_awaiting_human:
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
                wt = _wf._ensure_worktree(
                    spec, issue.number,
                    branch=_wf._resolve_branch_name(state, spec, issue.number),
                )
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
        # reviewer-side park OR review_cap: fall through to the
        # awaiting-human branch below, which will consume the human's
        # "retry" / `/orchestrator add-review-rounds` comment, clear the
        # park flags, and re-spawn the reviewer.

    # Awaiting-human path: human replied after a park; resume the developer
    # codex with their feedback. Identical mechanic to implementing's resume,
    # but on a clean pushed fix we bump the round while staying on
    # `validating` (no relabel emitted) so the reviewer re-evaluates the
    # new head on the next tick. Docs are not run here; they are deferred
    # to the final-docs handoff after reviewer approval.
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
        park_reason = state.get("park_reason")
        # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to
        # the `_sync_pr_worktree_to_base` retry loop -- the operator's
        # new comment is the "retry the rebase" signal, NOT a dev /
        # reviewer trigger for this stage. Stay silent so the refresh
        # keeps ownership of the comment; resuming the dev or
        # respawning the reviewer here would consume the comment as
        # input it has no context for and silently drop the retry
        # intent.
        if park_reason in _wf._AUTO_REBASE_PARK_REASONS:
            return
        last_action_id = state.get("last_action_comment_id")
        new_comments = gh.comments_after(issue, last_action_id)
        # `/orchestrator add-review-rounds N` operator command. Only honored
        # on a `review_cap` park: the cap has consumed every review round and
        # plain resuming the dev would re-park on the same cap next tick (the
        # original bug -- the round bump in the resume branch just trips
        # `round_n >= MAX_REVIEW_ROUNDS` again). On other parks the human's
        # reply IS the input the dev / reviewer needs, so we don't intercept
        # it. On a non-command reply while parked on the cap we stay parked
        # silently rather than waking the dev on a do-nothing prompt.
        if park_reason == "review_cap":
            if not new_comments:
                return
            cmd = _parse_add_review_rounds(new_comments)
            if cmd is None:
                return
            consumed_max = max(c.id for c in new_comments)
            state.set("last_action_comment_id", consumed_max)
            n, err = cmd
            if err is not None:
                _wf._post_issue_comment(
                    gh, issue, state,
                    f":warning: `/orchestrator add-review-rounds` ignored: "
                    f"{err}.",
                )
                gh.write_pinned_state(issue, state)
                return
            new_round = max(0, config.MAX_REVIEW_ROUNDS - n)
            state.set("review_round", new_round)
            state.set("awaiting_human", False)
            state.set("park_reason", None)
            _wf._post_issue_comment(
                gh, issue, state,
                f":arrows_counterclockwise: review-cap reset: granting {n} "
                f"more round(s) "
                f"(`review_round`={new_round}/{config.MAX_REVIEW_ROUNDS}); "
                "rerunning reviewer.",
            )
            # Fall through to the reviewer-spawn block below so the
            # reviewer reruns on the same tick (parity with the
            # reviewer_timeout / reviewer_failed branch). The block reads
            # `review_round` again from state.
        elif (
            not new_comments
            and park_reason in _VALIDATING_TRANSIENT_PARK_REASONS
        ):
            recovery = _try_recover_validating_transient_park(
                spec, issue, state
            )
            if recovery == "stuck":
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
        elif (
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
                wt = _wf._ensure_worktree(
                    spec, issue.number,
                    branch=_wf._resolve_branch_name(state, spec, issue.number),
                )
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
            f"{round_n} round(s); manual intervention needed. To grant "
            "more rounds without losing the PR/worktree, reply with "
            "`/orchestrator add-review-rounds N` "
            "(N = additional rounds, e.g. `1`).",
            reason="review_cap",
        )
        # Persist the park reason so the next tick's awaiting-human
        # branch can route the operator's `/orchestrator add-review-rounds`
        # comment through the cap-reset path (it gates on
        # `park_reason == "review_cap"`). `_park_awaiting_human` clears
        # `park_reason` to None by contract (the `reason=` kwarg only
        # feeds the audit event), so callers that need a transient or
        # cap-specific reason re-set it themselves.
        state.set("park_reason", "review_cap")
        gh.write_pinned_state(issue, state)
        return

    wt = _wf._ensure_worktree(
        spec, issue.number,
        branch=_wf._resolve_branch_name(state, spec, issue.number),
    )
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
        agent_spec=config.REVIEW_AGENT_SPEC,
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
        # Local verification gate: run the configured `VERIFY_COMMANDS` in
        # the per-issue worktree before posting the approval or relabeling
        # to `in_review`. Default-empty `VERIFY_COMMANDS` short-circuits to
        # "ok" so the legacy "no verification" behaviour is unchanged. A
        # failed / timed-out command, or a dirty tree left behind, parks
        # awaiting_human in `validating` with a stable `park_reason` so the
        # operator can fix the breakage. The verify gate is the first gate
        # after the reviewer agent so an obviously-broken branch never
        # reaches `in_review`; GitHub CI still runs against the PR for the
        # human merging it.
        verify = _wf._run_verify_commands(
            wt, config.VERIFY_COMMANDS, config.VERIFY_TIMEOUT,
        )
        if verify.status != "ok":
            _park_verify_failure(gh, issue, state, verify)
            gh.write_pinned_state(issue, state)
            return

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
        # human can adjudicate.
        squashed_count = 0
        if config.SQUASH_ON_APPROVAL:
            success, _sha_after, n_squashed, err = _wf._squash_and_force_push(
                spec, wt, _wf._resolve_branch_name(state, spec, issue.number), issue,
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
            squashed_count = n_squashed

        if pr_number is not None:
            # Seed the in_review comment watermark so `_handle_in_review`
            # does not replay the orchestrator's own automated comments
            # ("picking this up", "PR opened", the approval just posted)
            # as fresh PR feedback once the debounce expires.
            try:
                pr = gh.get_pr(int(pr_number))
            except Exception as e:
                # Recoverable: the in_review handler will fall back to its
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
        # Route through `documenting` for a final docs pass on the
        # approved (and possibly squashed) head before in_review picks
        # up. `_handle_documenting`'s success exits always advance to
        # `in_review`. The PR watermarks, approval comment, and squash
        # comment seeded here are preserved across the documenting hop
        # unchanged.
        gh.set_workflow_label(issue, WorkflowLabel.DOCUMENTING)
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
    # The dev-fix subphase runs under the `fixing` label so the active job
    # is observably "fixing reviewer-requested changes" rather than
    # "validating" (which would now read as reviewer/verify work only). On
    # a successful pushed fix we relabel back to `validating` so the
    # reviewer re-evaluates the new head next tick; on any park the issue
    # stays on `fixing` and the fixing handler owns the awaiting-human
    # rescan + dev resume cycle (`fixing` already extends to "automated
    # reviewer feedback" by virtue of this route, in addition to its
    # original in_review human-feedback duty). `review_round` accounting,
    # `MAX_REVIEW_ROUNDS`, dev-session pinning, and the final-docs handoff
    # are unchanged -- only the visible label moves with the active work.
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

    # Flip to `fixing` BEFORE spawning the dev so the GitHub label reflects
    # the active job for the duration of the dev subprocess (and for any
    # subsequent ticks if the run parks). The label set is independent of
    # the pinned-state write below; doing it first means a crash inside the
    # dev spawn still leaves the issue on `fixing` with stale awaiting_human
    # =False, which the next tick's fixing handler treats as no-feedback
    # and bounces back to `validating` so the reviewer reruns. Without the
    # pre-spawn flip, a crash would leave a stale `validating` label over
    # work the reviewer never produced a verdict for.
    gh.set_workflow_label(issue, WorkflowLabel.FIXING)
    gh.write_pinned_state(issue, state)

    fix_prompt = _wf._build_fix_prompt(feedback)
    before_sha = _wf._head_sha(wt)
    dev_spec, dev_backend, dev_args, dev_sid = _wf._read_dev_session(state)
    dev_result = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="developer",
        stage="fixing",
        backend=dev_backend,
        prompt=fix_prompt,
        cwd=wt,
        agent_spec=dev_spec,
        resume_session_id=dev_sid,
        extra_args=dev_args,
        review_round=round_n,
        retry_count=state.get("retry_count"),
    )
    state.set("last_agent_action_at", _wf._now_iso())

    if not _handle_dev_fix_result(
        gh, spec, issue, state, wt, dev_result, before_sha
    ):
        # Park (timeout / no-commit / dirty / push-fail): the issue stays
        # on `fixing` so the next tick's `_handle_fixing` owns the
        # awaiting-human rescan. The fixing handler's filter drops the
        # orchestrator's own reviewer-feedback PR comment and park
        # comment, so an awaiting_human=True tick with no new human reply
        # returns silently rather than bouncing back to `validating`.
        gh.write_pinned_state(issue, state)
        return

    # Pushed fix: bump the round and hand back to `validating` so the
    # reviewer re-evaluates the new head next tick.
    state.set("review_round", round_n + 1)
    gh.set_workflow_label(issue, WorkflowLabel.VALIDATING)
    gh.write_pinned_state(issue, state)
