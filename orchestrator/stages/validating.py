# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating stage handlers and reviewer-session lifecycle.

Owns `_handle_validating` plus the reviewer-side primitives the rest of
the workflow re-uses: post-agent dev-fix disposition (`_handle_dev_fix_result`,
with its stranded-fix deferred-publish gate `_stranded_fix_unpushed`,
which the fixing handler's ACK fast path also consults via the
workflow facade),
post-resume disposition for a user-content-change dev resume
(`_post_user_content_change_result`), the validating-side transient-park
recovery (`_try_recover_validating_transient_park` plus its
`_VALIDATING_TRANSIENT_PARK_REASONS` set), and the validating->in_review
handoff watermark seeding (`_seed_watermark_past_self`,
`_latest_pr_comment_ids`, and the per-PR seed
`_seed_in_review_pr_watermarks`).

`_handle_validating` itself is a thin dispatcher over stage-private
sub-handlers -- terminal-finalization guards
(`_finalize_validating_terminal`), the user-content drift resume
(`_resume_dev_on_validating_drift`), awaiting-human routing
(`_handle_validating_awaiting_human`), verdict routing
(`_finalize_validating_approval` with its in_review handoff seeding
`_seed_in_review_handoff_watermarks` + the pure `_ratchet_watermark`,
`_park_reviewer_no_verdict`, `_handle_validating_changes_requested`), and
the pure verify-failure formatter `_verify_failure_detail`. These stay
internal to this module and are reached directly, not through the facade.

ALL workflow-owned helpers (`_park_awaiting_human`, `_run_agent_tracked`,
`_now_iso`, the worktree plumbing, the drift / manifest / messaging
helpers re-exported into `workflow`) are reached through the parent
module via `from orchestrator import workflow as _wf` at call time. The
compatibility surface tests rely on -- `patch.object(workflow, "_foo")`
-- has to keep working from inside the stage module too, so the
handlers must NOT direct-import these names from `workflow_drift` /
`workflow_messages` / `worktrees`; doing so would bind a stable
reference that test patches against `workflow.X` could not affect.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any, Optional, Tuple

from github.Issue import Issue

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.comment_trust import filter_trusted
from orchestrator.state_machine import WorkflowLabel
from orchestrator.github import GitHubClient, PinnedState


# Operator escape hatch for `park_reason=review_cap`. Resets the review
# loop without losing the PR/worktree (see `_handle_validating`). The
# command lives in the issue thread because the cap-park message lands
# there, and is anchored to start-of-line so prose like "we should run
# `/orchestrator add-review-rounds 2`" cannot fire it accidentally.
_ADD_REVIEW_ROUNDS_RE = re.compile(
    r"^\s*/orchestrator\s+add-review-rounds\s+(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class _ReviewerRun:
    wt: Path
    round_n: int
    pr_number: Any
    agent_result: AgentResult


@dataclass(frozen=True)
class _ReviewerDecision:
    run: _ReviewerRun
    verdict: str
    body: str

    @property
    def feedback(self) -> str:
        return (
            self.body.strip()
            or (self.run.agent_result.last_message or "").strip()
        )


@dataclass(frozen=True)
class _DevFixRun:
    worktree: Path
    agent_result: AgentResult
    before_sha: str
    after_sha: Optional[str] = None


@dataclass(frozen=True)
class _RequestedChanges:
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    decision: _ReviewerDecision


def _dev_fix_run(context_args: tuple, fields: dict) -> tuple[PinnedState, _DevFixRun]:
    if len(context_args) != 4:
        raise TypeError("expected state, worktree, result, and before_sha")
    state, worktree, agent_result, before_sha = context_args
    unknown = set(fields) - {"after_sha"}
    if unknown:
        raise TypeError(f"unexpected fix-result option(s): {sorted(unknown)!r}")
    return state, _DevFixRun(
        worktree, agent_result, before_sha, fields.get("after_sha"),
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
    for comment in reversed(comments):
        body = comment.body or ""
        command_match = _ADD_REVIEW_ROUNDS_RE.search(body)
        if not command_match:
            continue
        additional_rounds = int(command_match.group(1))
        if additional_rounds <= 0:
            return (
                additional_rounds,
                f"expected a positive integer (got `{additional_rounds}`)",
            )
        return (additional_rounds, None)
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
# Pinned-state keys, park-reason values, and handler-outcome tokens this stage
# reads and writes.
_PARK_REASON = "park_reason"
_PRE_DEV_FIX_SHA = "pre_dev_fix_sha"
_REVIEW_ROUND = "review_round"
_REASON_PUSH_FAILED = "push_failed"
_REASON_AGENT_TIMEOUT = "agent_timeout"
_REASON_REVIEWER_TIMEOUT = "reviewer_timeout"
_REASON_REVIEWER_FAILED = "reviewer_failed"
_REASON_REVIEW_CAP = "review_cap"
_OUTCOME_PARKED = "parked"
_OUTCOME_PUSHED = "pushed"
_OUTCOME_STUCK = "stuck"
_OUTCOME_RETURN = "return"
# Characters of a commit SHA shown in operator-facing verify diagnostics.
_SHORT_SHA_LEN = 12


_VALIDATING_TRANSIENT_PARK_REASONS = frozenset(
    (_REASON_PUSH_FAILED, _REASON_AGENT_TIMEOUT, _REASON_REVIEWER_TIMEOUT, _REASON_REVIEWER_FAILED)
)


def _stranded_fix_unpushed(
    spec: config.RepoSpec, wt: Path, state: PinnedState, issue: Issue
) -> bool:
    """True when a clean worktree HEAD is strictly ahead of the remote PR
    branch -- a fix an earlier parked run committed but never published.

    The shape arises when the publish was blocked at commit time (e.g. a
    dirty-worktree park whose stray files a human later had the dev clean
    up): every later resume sees `after_sha == before_sha`, so without
    this check the stranded commit can never reach the PR and the issue
    ping-pongs between `awaiting_human` parks forever.

    Conservative by construction: a dirty tree, a failed fetch, or a
    remote that moved (`behind > 0` -- pushing would race a head we have
    not reconciled) all report False so the caller falls back to the
    question park instead of pushing blind.
    """
    from orchestrator import workflow as _wf

    if _wf._worktree_dirty_files(wt):
        return False
    branch = _wf._resolve_branch_name(state, spec, issue.number)
    fetch = _wf._authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=wt,
    )
    if fetch.returncode != 0:
        return False
    ahead, behind = _wf._branch_ahead_behind(spec, wt, branch)
    return ahead > 0 and behind == 0


def _park_dev_fix_timeout(
    gh: GitHubClient, issue: Issue, state: PinnedState, before_sha: str,
) -> None:
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent timed out after {config.AGENT_TIMEOUT}s, "
        "manual intervention needed.",
        reason=_REASON_AGENT_TIMEOUT,
    )
    state.set(_PARK_REASON, _REASON_AGENT_TIMEOUT)
    state.set(_PRE_DEV_FIX_SHA, before_sha or "")


def _dev_fix_is_publishable(
    spec: config.RepoSpec, issue: Issue, state: PinnedState, run: _DevFixRun,
) -> bool:
    from orchestrator import workflow as _wf

    after_sha = run.after_sha
    if after_sha is None:
        after_sha = _wf._head_sha(run.worktree)
    if after_sha and after_sha != run.before_sha:
        return True
    return bool(after_sha) and _stranded_fix_unpushed(
        spec, run.worktree, state, issue,
    )


def _publish_dev_fix(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run: _DevFixRun,
) -> bool:
    from orchestrator import workflow as _wf

    state.set("silent_park_count", 0)
    dirty = _wf._worktree_dirty_files(run.worktree)
    if dirty:
        _wf._on_dirty_worktree(gh, issue, state, run.agent_result, dirty)
        return False
    branch = _wf._resolve_branch_name(state, spec, issue.number)
    if _wf._push_branch(spec, run.worktree, branch):
        return True
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
        reason=_REASON_PUSH_FAILED,
    )
    state.set(_PARK_REASON, _REASON_PUSH_FAILED)
    return False


def _dispose_dev_fix_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run: _DevFixRun,
) -> bool:
    from orchestrator import workflow as _wf

    if run.agent_result.interrupted:
        return False
    if run.agent_result.timed_out:
        _park_dev_fix_timeout(gh, issue, state, run.before_sha)
        return False
    if not _dev_fix_is_publishable(spec, issue, state, run):
        _wf._on_question(gh, issue, state, run.agent_result)
        return False
    return _publish_dev_fix(gh, spec, issue, state, run)


def _handle_dev_fix_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    *context_args,
    **fields,
) -> bool:
    """Post-agent handling for a dev fix during validating.

    Returns True if a fix was committed, pushed, and the caller should
    advance the label (validating routes the issue back to `validating`
    on True so the reviewer re-runs against the new head; any stale
    approval state must be reset by the caller before relabeling). A
    no-new-commit run also returns True when it published a stranded fix
    a prior parked run had committed (see `_stranded_fix_unpushed`).
    Returns False if the run produced no fix (timeout, no-new-commit,
    dirty tree, or push failure); caller should write state and return.
    A shutdown-killed (interrupted) run also returns False WITHOUT parking,
    posting, or publishing, so the next tick re-runs the dev cleanly.

    `after_sha`, when provided, is the post-agent HEAD the caller already
    read (e.g. the fixing handler's ACK fast path); passing it avoids a
    redundant `_head_sha` call. When None it is read here.
    """
    state, run = _dev_fix_run(context_args, fields)
    return _dispose_dev_fix_result(gh, spec, issue, state, run)


def _post_drift_ack(
    gh: GitHubClient, issue: Issue, state: PinnedState, reason: str,
) -> None:
    from orchestrator import workflow as _wf

    quoted = _wf._as_blockquote(reason)
    _wf._post_issue_comment(
        gh, issue, state,
        ":speech_balloon: dev session reports the existing work "
        f"satisfies the edit:\n\n{quoted}",
    )
    state.set("silent_park_count", 0)


def _dispose_user_content_change_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run: _DevFixRun,
) -> str:
    from orchestrator import workflow as _wf

    if run.agent_result.interrupted:
        return _OUTCOME_PARKED
    if run.agent_result.timed_out:
        _park_dev_fix_timeout(gh, issue, state, run.before_sha)
        return _OUTCOME_PARKED
    if not _dev_fix_is_publishable(spec, issue, state, run):
        ack_reason = _wf._drift_ack_reason(
            run.agent_result.last_message or "",
        )
        if ack_reason:
            _post_drift_ack(gh, issue, state, ack_reason)
            return "ack"
        _wf._on_question(gh, issue, state, run.agent_result)
        return _OUTCOME_PARKED
    return (
        _OUTCOME_PUSHED if _publish_dev_fix(gh, spec, issue, state, run)
        else _OUTCOME_PARKED
    )


def _post_user_content_change_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    *context_args,
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
    * ``"pushed"`` -- new commit landed and the push succeeded, OR this
      no-commit run found a committed-but-unpublished fix stranded on the
      branch by a prior parked / interrupted resume and published it (the
      stranded-fix gate, mirroring `_handle_dev_fix_result`).
      Validating stays on `validating` (and bumps `review_round`) so
      the reviewer re-evaluates the new head; in_review also hands
      straight back to `validating`. Docs are not run on this exit --
      the single docs pass is deferred to the final-docs handoff after
      reviewer approval. Any stale approval state must be reset by
      the caller before relabeling.
    * ``"parked"`` -- timeout, dirty tree, push fail, silent crash
      (empty `last_message`), OR a no-commit response WITHOUT the
      `ACK:` marker (treated as a clarification question via
      `_on_question`). State already carries the park flags. A
      shutdown-killed (interrupted) run also returns ``"parked"`` but
      WITHOUT setting any park flags or posting -- the run is ignored
      and the next tick retries the resume.

    The explicit `ACK:` marker is required because a generic non-empty
    no-commit response is often a clarification question, not an
    acknowledgement; swallowing it as an ack would post a misleading
    "existing work satisfies" comment AND continue the workflow with
    `awaiting_human=False`, stranding the real question.
    """
    state, run = _dev_fix_run(context_args, {})
    return _dispose_user_content_change_result(gh, spec, issue, state, run)


def _bump_review_round(state: PinnedState) -> None:
    current_round = int(state.get(_REVIEW_ROUND) or 0)
    state.set(_REVIEW_ROUND, current_round + 1)


def _recover_failed_push(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> str:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    if not worktree.exists():
        return _OUTCOME_STUCK
    branch = _wf._resolve_branch_name(state, spec, issue.number)
    if not _wf._push_branch(spec, worktree, branch):
        return _OUTCOME_STUCK
    _bump_review_round(state)
    return _OUTCOME_PUSHED


def _recover_timed_out_fix(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> str:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    if not worktree.exists() or _wf._worktree_dirty_files(worktree):
        return _OUTCOME_STUCK
    before_sha = state.get(_PRE_DEV_FIX_SHA)
    if not isinstance(before_sha, str):
        return _OUTCOME_STUCK
    current_sha = _wf._head_sha(worktree)
    if not current_sha or current_sha == before_sha:
        state.set(_PRE_DEV_FIX_SHA, None)
        return "cleared"
    branch = _wf._resolve_branch_name(state, spec, issue.number)
    if not _wf._push_branch(spec, worktree, branch):
        return _OUTCOME_STUCK
    state.set(_PRE_DEV_FIX_SHA, None)
    _bump_review_round(state)
    return _OUTCOME_PUSHED


def _try_recover_validating_transient_park(
    spec: config.RepoSpec, issue: Issue, state: PinnedState
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
    park_reason = state.get(_PARK_REASON)
    if park_reason == _REASON_PUSH_FAILED:
        return _recover_failed_push(spec, issue, state)
    if park_reason in (_REASON_REVIEWER_TIMEOUT, _REASON_REVIEWER_FAILED):
        return "cleared"
    if park_reason == _REASON_AGENT_TIMEOUT:
        return _recover_timed_out_fix(spec, issue, state)
    return _OUTCOME_STUCK


def _watermark_comment_pairs(
    issue_comments: list, pr_comments: list,
) -> list[Tuple[Any, bool]]:
    return sorted(
        [(comment, True) for comment in issue_comments]
        + [(comment, False) for comment in pr_comments],
        key=lambda pair: pair[0].id,
    )


def _is_orchestrator_comment(comment, orchestrator_ids: set[int]) -> bool:
    from orchestrator import workflow as _wf

    return (
        comment.id in orchestrator_ids
        or _wf._ORCH_COMMENT_MARKER in (getattr(comment, "body", None) or "")
    )


@dataclass
class _WatermarkWalker:
    orchestrator_ids: set[int]
    pickup_comment_id: int
    consumed_through: Optional[int]
    watermark: Optional[int] = None
    seen_self: bool = False

    def consume(self, comment, is_issue_thread: bool) -> bool:
        is_self = _is_orchestrator_comment(comment, self.orchestrator_ids)
        already_consumed = (
            is_issue_thread
            and self.consumed_through is not None
            and comment.id <= self.consumed_through
        )
        if is_self:
            self.watermark = comment.id
            self.seen_self = True
        elif not self.seen_self and comment.id < self.pickup_comment_id:
            self.watermark = comment.id
        elif already_consumed:
            self.watermark = comment.id
        else:
            return False
        return True


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
    if pickup_comment_id is None:
        # Legacy state without a pickup anchor: refuse to advance. We
        # cannot tell pre-pickup chatter (safe to skip) from human feedback
        # posted during implementing/validating (must preserve), and
        # dropping a human comment is the unsafe direction.
        return None
    # Tag each comment with its surface so the walk below can apply
    # `consumed_through` to the issue thread only.
    comment_pairs = _watermark_comment_pairs(
        issue_thread_comments, pr_conversation_comments,
    )
    if not any(
        _is_orchestrator_comment(comment, orchestrator_ids)
        for comment, _ in comment_pairs
    ):
        return None
    walker = _WatermarkWalker(
        orchestrator_ids, pickup_comment_id, consumed_through,
    )
    for comment, is_issue_thread in comment_pairs:
        if not walker.consume(comment, is_issue_thread):
            break
    return walker.watermark


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
    from orchestrator import workflow as _wf

    orchestrator_ids = _wf._orchestrator_ids(state)
    # `last_action_comment_id` doubles as a "consumed through" marker:
    # both park comments and post-resume bumps land here, so any issue
    # comment with id <= this value has either been posted by the
    # orchestrator (filtered by `orchestrator_comment_ids`) or already
    # been fed to the dev session (must not replay).
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
            orchestrator_ids, _state_int(state, "pickup_comment_id"),
            consumed_through=_state_int(state, "last_action_comment_id"),
        ),
        None,
    )


def _state_int(state: PinnedState, key: str) -> Optional[int]:
    state_value = state.get(key)
    return state_value if isinstance(state_value, int) else None


_VERIFY_STATUS_TO_REASON = {
    "failed": "verify_failed",
    "timeout": "verify_timeout",
    "dirty": "verify_dirty",
    "head_changed": "verify_head_changed",
}


def _verify_failure_detail(verify) -> str:
    """One-line description of a non-ok local-verify result, naming the
    failing command and its failure mode.

    The `head_changed` branch surfaces both short SHAs so the operator can
    `git show` the stray commit and decide whether to keep it (re-spawn the
    reviewer on the new HEAD) or revert it before re-trying.
    """
    if verify.status == "timeout":
        return (
            f"`{verify.command}` timed out after "
            f"{config.VERIFY_TIMEOUT}s"
        )
    if verify.status == "dirty":
        files = ", ".join(
            f"`{file_path}`" for file_path in verify.dirty_files[:10]
        )
        if len(verify.dirty_files) > 10:
            elided = len(verify.dirty_files) - 10
            files = f"{files}, … (+{elided} more)"
        return f"`{verify.command}` left the worktree dirty: {files}"
    if verify.status == "head_changed":
        before = (verify.head_before or "")[:_SHORT_SHA_LEN] or "(no HEAD)"
        after = (verify.head_after or "")[:_SHORT_SHA_LEN] or "(no HEAD)"
        return (
            f"`{verify.command}` moved HEAD ({before} -> {after}); "
            "verify commands must not commit"
        )
    exit_display = "?" if verify.exit_code is None else verify.exit_code
    return (
        f"`{verify.command}` exited with code "
        f"{exit_display}"
    )


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
    from orchestrator import workflow as _wf

    reason = _VERIFY_STATUS_TO_REASON.get(verify.status, "verify_failed")
    detail = _verify_failure_detail(verify)

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
        quoted = _wf._as_blockquote(output.rstrip())
        message = f"{message}\n\n_Verify output (tail):_\n\n{quoted}"

    _wf._park_awaiting_human(gh, issue, state, message, reason=reason)
    state.set(_PARK_REASON, reason)


def _ratchet_watermark(prev, seeded):
    """Combine a previously-persisted in_review watermark with a freshly-seeded
    one, never moving backward.

    A prior in_review tick may have already advanced the persisted watermark
    past PR feedback the dev has since fixed; `_seed_watermark_past_self` stops
    at the first post-pickup human comment, so without the max() that consumed
    comment would replay as "new". Returns the max of the two when both are
    present, the one that exists otherwise, or 0 when neither does -- 0 means
    "scan all from the beginning" and marks the surface as already seeded so the
    in_review legacy migration does not advance past historical human feedback.
    """
    if isinstance(prev, int):
        return prev if seeded is None else max(seeded, prev)
    return 0 if seeded is None else seeded


def _finalize_validating_terminal(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> bool:
    """Terminal short-circuits checked before the reviewer runs; True when one
    fired and the caller must return.

    External merge: a human merged the PR while the reviewer was queued.
    Finalize to `done` rather than running the reviewer against a branch that
    already landed. Closed-issue counterpart: the closed-`validating` sweep
    yields issues a human closed without a merged PR (the change was rejected
    mid-review, or the PR was closed-without-merge); flip to `rejected` so the
    reviewer does not spawn against a closed issue and the PR is not relabeled
    back to `in_review`. The in_review / fixing handlers carry equivalent
    terminal checks.
    """
    from orchestrator import workflow as _wf

    if _wf._finalize_if_pr_merged(gh, spec, issue, state):
        return True
    if _wf._finalize_if_issue_closed(gh, spec, issue, state):
        return True
    return False


@dataclass(frozen=True)
class _ValidatingDriftRun:
    worktree: Path
    agent_result: AgentResult
    before_sha: str
    paused: bool


def _run_validating_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> _ValidatingDriftRun:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    if not worktree.exists():
        worktree = _wf._ensure_worktree(
            spec,
            issue.number,
            branch=_wf._resolve_branch_name(state, spec, issue.number),
        )
    before_sha = _wf._head_sha(worktree)
    followup = _wf._build_user_content_change_prompt(
        issue, _wf._recent_comments_text(issue),
    )
    worktree, agent_result, paused = _wf._resume_dev_with_text(
        gh, spec, issue, state, followup, pause_guard=True,
    )
    return _ValidatingDriftRun(worktree, agent_result, before_sha, paused)


def _defer_validating_drift(state: PinnedState) -> bool:
    return bool(
        state.get("awaiting_human")
        and state.get(_PARK_REASON)
        in (_REASON_REVIEWER_TIMEOUT, _REASON_REVIEWER_FAILED, _REASON_REVIEW_CAP)
    )


def _finish_validating_drift(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    run: _ValidatingDriftRun,
) -> None:
    outcome = _post_user_content_change_result(
        gh,
        spec,
        issue,
        state,
        run.worktree,
        run.agent_result,
        run.before_sha,
    )
    if run.agent_result.interrupted:
        return
    if outcome == _OUTCOME_PUSHED:
        _bump_review_round(state)
    gh.write_pinned_state(issue, state)


def _resume_dev_on_validating_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> bool:
    """Resume the dev session when a human edited the issue title/body while the
    reviewer was running.

    Re-decomposing now would discard the dev's already-pushed work, so notify
    the human, resume the dev session on its locked backend with the new body,
    and on a successful pushed fix bump `review_round` while staying on
    `validating` (no relabel emitted) so the reviewer re-evaluates the updated
    body + new diff on the next tick. An ACK reply (no commit) keeps the issue
    on `validating`. On a failed resume (timeout, dirty, no commit), the
    standard park flags land via `_post_user_content_change_result`.

    Returns True when a drift was detected and fully handled (caller must
    return). Returns False when there is no drift, or when the issue is parked
    with a reviewer-side reason (`reviewer_timeout` / `reviewer_failed`) or on
    the review-round cap (`review_cap`) -- those defer to the awaiting-human
    branch. A human "retry" comment on a reviewer-side park must re-spawn the
    REVIEWER, not the dev: the failure produced no review output for the dev to
    act on, and the reviewer re-reads the updated `issue.body` + comments via
    `_build_review_prompt` when it runs. For `review_cap`, the cap has consumed
    every round, so resuming the dev would re-park on the cap next tick; the
    operator's `/orchestrator add-review-rounds` command lives in the
    awaiting-human branch, and the command comment itself bumps the user-content
    hash, so without this bypass the drift block would fire first and the
    command would never be parsed. The new baseline hash is persisted here
    either way so the next tick's drift check has a stable comparison point.
    """
    from orchestrator import workflow as _wf

    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is None:
        return False
    state.set("user_content_hash", new_hash)
    if _defer_validating_drift(state):
        return False

    _wf._post_issue_comment(
        gh, issue, state,
        ":pencil2: issue body changed; resuming dev session.",
    )
    # Mark the full issue thread as consumed: the dev sees it via
    # `_recent_comments_text` in the resume prompt, so the eventual
    # handoff to in_review must not replay those comments as fresh
    # feedback. Mirrors `_resume_developer_on_human_reply`'s pre-spawn bump.
    _wf._mark_drift_comments_consumed(gh, issue, state)
    run = _run_validating_drift(gh, spec, issue, state)
    state.set("last_agent_action_at", _wf._now_iso())
    if run.paused:
        # Live pause applied during the drift resume: the helper already
        # stopped before persisting the session id or clearing
        # `awaiting_human`. Return WITHOUT running the result handler (which
        # would post / push / advance the round) or writing pinned state, so
        # the drift bookkeeping staged above stays unrecorded and the committed
        # work stays on the branch; the next tick re-detects the drift once the
        # label is removed.
        return True
    # Custom result handler: a no-commit-with-message reply is the dev
    # confirming the existing work already satisfies the edit, and the resume
    # prompt explicitly invites that response. `_handle_dev_fix_result` would
    # park on it via `_on_question`; use the user-content-specific helper so a
    # harmless clarification does not stall the issue.
    _finish_validating_drift(gh, spec, issue, state, run)
    return True


@dataclass(frozen=True)
class _AwaitingValidation:
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    park_reason: Any
    comments: list

    @classmethod
    def build(
        cls, gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
    ) -> _AwaitingValidation:
        return cls(
            gh,
            spec,
            issue,
            state,
            state.get(_PARK_REASON),
            filter_trusted(
                gh.comments_after(issue, state.get("last_action_comment_id")),
            ),
        )

    def clear_park(self) -> None:
        self.state.set("awaiting_human", False)
        self.state.set(_PARK_REASON, None)

    def consume_comments(self) -> None:
        self.state.set(
            "last_action_comment_id",
            max(comment.id for comment in self.comments),
        )


def _review_cap_awaiting_action(
    context: _AwaitingValidation,
) -> Optional[str]:
    from orchestrator import workflow as _wf

    if context.park_reason != _REASON_REVIEW_CAP:
        return None
    if not context.comments:
        return _OUTCOME_RETURN
    command = _parse_add_review_rounds(context.comments)
    if command is None:
        return _OUTCOME_RETURN
    context.consume_comments()
    additional_rounds, error = command
    if error is not None:
        _wf._post_issue_comment(
            context.gh,
            context.issue,
            context.state,
            f":warning: `/orchestrator add-review-rounds` ignored: {error}.",
        )
        context.gh.write_pinned_state(context.issue, context.state)
        return _OUTCOME_RETURN
    new_round = max(0, config.MAX_REVIEW_ROUNDS - additional_rounds)
    context.state.set(_REVIEW_ROUND, new_round)
    context.clear_park()
    _wf._post_issue_comment(
        context.gh,
        context.issue,
        context.state,
        f":arrows_counterclockwise: review-cap reset: granting "
        f"{additional_rounds} more round(s) "
        f"(`review_round`={new_round}/{config.MAX_REVIEW_ROUNDS}); "
        "rerunning reviewer.",
    )
    return "spawn_reviewer"


def _transient_awaiting_action(
    context: _AwaitingValidation,
) -> Optional[str]:
    if (
        context.comments
        or context.park_reason not in _VALIDATING_TRANSIENT_PARK_REASONS
    ):
        return None
    recovery = _try_recover_validating_transient_park(
        context.spec, context.issue, context.state,
    )
    if recovery != _OUTCOME_STUCK:
        context.clear_park()
        context.gh.write_pinned_state(context.issue, context.state)
    return _OUTCOME_RETURN


def _reviewer_retry_awaiting_action(
    context: _AwaitingValidation,
) -> Optional[str]:
    if not context.comments or context.park_reason not in (
        _REASON_REVIEWER_TIMEOUT, _REASON_REVIEWER_FAILED,
    ):
        return None
    context.consume_comments()
    context.clear_park()
    return "spawn_reviewer"


@dataclass(frozen=True)
class _AwaitingDevAttempt:
    run: _DevFixRun
    paused: bool


def _resume_awaiting_dev_agent(
    context: _AwaitingValidation, continue_action: str,
) -> Optional[tuple[Path, AgentResult, bool]]:
    from orchestrator import workflow as _wf

    if continue_action != "retry":
        return _wf._resume_developer_on_human_reply(
            context.gh,
            context.spec,
            context.issue,
            context.state,
            pause_guard=True,
        )
    context.consume_comments()
    followup = f"{_wf._CONTINUE_RETRY_PROMPT}\n\n{_wf._FOREGROUND_ONLY_NOTE}"
    return _wf._resume_dev_with_text(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        followup,
        pause_guard=True,
    )


def _run_awaiting_dev(
    context: _AwaitingValidation, continue_action: str,
) -> Optional[_AwaitingDevAttempt]:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(context.spec, context.issue.number)
    if not worktree.exists():
        worktree = _wf._ensure_worktree(
            context.spec,
            context.issue.number,
            branch=_wf._resolve_branch_name(
                context.state, context.spec, context.issue.number,
            ),
        )
    before_sha = _wf._head_sha(worktree)
    resumed = _resume_awaiting_dev_agent(context, continue_action)
    if resumed is None:
        return None
    return _AwaitingDevAttempt(
        _DevFixRun(resumed[0], resumed[1], before_sha), resumed[2],
    )


def _resume_validating_awaiting_dev(context: _AwaitingValidation) -> str:
    from orchestrator import workflow as _wf

    continue_action = (
        _wf._continue_command_action(context.comments, context.park_reason)
        if context.comments else "passthrough"
    )
    if continue_action == "refuse":
        _wf._refuse_parked_continue(context.gh, context.issue, context.state)
        context.gh.write_pinned_state(context.issue, context.state)
        return _OUTCOME_RETURN
    attempt = _run_awaiting_dev(context, continue_action)
    if attempt is None:
        return _OUTCOME_RETURN
    context.state.set("last_agent_action_at", _wf._now_iso())
    if attempt.paused:
        return _OUTCOME_RETURN
    pushed = _handle_dev_fix_result(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        attempt.run.worktree,
        attempt.run.agent_result,
        attempt.run.before_sha,
    )
    if not pushed:
        if not attempt.run.agent_result.interrupted:
            context.gh.write_pinned_state(context.issue, context.state)
        return _OUTCOME_RETURN
    _bump_review_round(context.state)
    context.gh.write_pinned_state(context.issue, context.state)
    return _OUTCOME_RETURN


def _handle_validating_awaiting_human(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> str:
    """Route an awaiting-human `validating` tick after a park.

    A human replied (or a transient condition self-resolved) while the issue
    was parked. Resume the developer with their feedback -- identical mechanic
    to implementing's resume, but on a clean pushed fix we bump the round while
    staying on `validating` (no relabel emitted) so the reviewer re-evaluates
    the new head next tick. Docs are deferred to the final-docs handoff after
    reviewer approval.

    Returns ``"return"`` when the tick is fully handled (caller must return) or
    ``"spawn_reviewer"`` when the park cleared into a reviewer re-run (review-cap
    reset, reviewer timeout / silent crash) and the caller should fall through
    to the round-cap check and reviewer spawn.
    """
    from orchestrator import workflow as _wf

    context = _AwaitingValidation.build(gh, spec, issue, state)

    # Transient-park recovery: when the original park reason is something
    # that can resolve without a human comment (a push race that the
    # next --force-with-lease push will land, or an agent timeout that
    # the next tick can simply rerun past), re-attempt silently. This
    # mirrors the in_review recovery branch -- without it, the issue
    # would sit forever, because `_resume_developer_on_human_reply`
    # only fires on new issue-thread comments and the human action
    # that unstuck the underlying condition typically does not include
    # one.
    # The refresh-time `_AUTO_REBASE_PARK_REASONS` parks belong to
    # the `_sync_pr_worktree_to_base` retry loop -- the operator's
    # new comment is the "retry the rebase" signal, NOT a dev /
    # reviewer trigger for this stage. Stay silent so the refresh
    # keeps ownership of the comment; resuming the dev or
    # respawning the reviewer here would consume the comment as
    # input it has no context for and silently drop the retry
    # intent.
    if context.park_reason in _wf._AUTO_REBASE_PARK_REASONS:
        return _OUTCOME_RETURN
    # `/orchestrator add-review-rounds N` operator command. Only honored
    # on a `review_cap` park: the cap has consumed every review round and
    # plain resuming the dev would re-park on the same cap next tick (the
    # original bug -- the round bump in the resume branch just trips
    # `round_n >= MAX_REVIEW_ROUNDS` again). On other parks the human's
    # reply IS the input the dev / reviewer needs, so we don't intercept
    # it. On a non-command reply while parked on the cap we stay parked
    # silently rather than waking the dev on a do-nothing prompt.
    for decision_helper in (
        _review_cap_awaiting_action,
        _transient_awaiting_action,
        _reviewer_retry_awaiting_action,
    ):
        action = decision_helper(context)
        if action is not None:
            return action
    return _resume_validating_awaiting_dev(context)


def _seed_in_review_handoff_watermarks(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    pr_number,
    squashed_count: int,
) -> None:
    """Seed the in_review comment watermarks so `_handle_in_review` does not
    replay the orchestrator's own automated comments ("picking this up",
    "PR opened", the approval just posted, the squash notice) as fresh PR
    feedback once the debounce expires.

    A get_pr failure is recoverable -- the in_review handler falls back to its
    legacy `last_action_comment_id` watermark -- so we log and return without
    seeding.
    """
    from orchestrator import workflow as _wf

    if pr_number is None:
        return
    try:
        pr = gh.get_pr(int(pr_number))
    except Exception as error:
        # Surface the failure but skip the traceback -- it adds no signal.
        _wf.log.warning(
            "issue=#%s could not snapshot PR #%s for in_review "
            "handoff: %s", issue.number, pr_number, error,
        )
        return
    # Post the squash PR comment BEFORE seeding watermarks so the seed walks
    # past it (its id lands in `orchestrator_comment_ids` via `_post_pr_comment`).
    # Without that ordering, the next in_review tick treats the squash comment
    # as fresh PR feedback once the debounce expires and resumes the dev
    # session over an informational orchestrator post.
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
    _seed_in_review_pr_watermarks(gh, issue, state, pr)


def _seed_in_review_pr_watermarks(
    gh: GitHubClient, issue: Issue, state: PinnedState, pr,
) -> None:
    """Seed the three in_review comment watermarks past the leading run of
    orchestrator-authored comments on `pr`'s surfaces.

    Used by validating's reviewer-approval handoff
    (`_seed_in_review_handoff_watermarks`) so `_handle_in_review` does not
    replay the orchestrator's own automated comments (pickup ping, "PR opened",
    approval, squash notice) as fresh PR feedback once the debounce expires.
    Concurrent human feedback posted during the prior stage is preserved:
    `_latest_pr_comment_ids` stops the seed walk at the first unread
    non-orchestrator comment, and `_ratchet_watermark` never regresses a
    watermark a prior in_review tick already advanced.

    Inline review comments and review summaries live in namespaces the
    orchestrator never posts on, so `_latest_pr_comment_ids` returns None for
    the inline surface and there is no seeded summary value; `_ratchet_watermark`
    defaults each to 0 so the in_review legacy migration treats them as already
    seeded and does NOT advance past human feedback submitted on those surfaces.
    """
    issue_wm, review_wm = _latest_pr_comment_ids(gh, issue, pr, state)
    state.set(
        "pr_last_comment_id",
        _ratchet_watermark(state.get("pr_last_comment_id"), issue_wm),
    )
    state.set(
        "pr_last_review_comment_id",
        _ratchet_watermark(state.get("pr_last_review_comment_id"), review_wm),
    )
    state.set(
        "pr_last_review_summary_id",
        _ratchet_watermark(state.get("pr_last_review_summary_id"), None),
    )


def _approved_work_verifies(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _ReviewerRun,
) -> bool:
    from orchestrator import workflow as _wf

    verify = _wf._run_verify_commands(
        reviewer_run.wt, config.VERIFY_COMMANDS, config.VERIFY_TIMEOUT,
    )
    if verify.status == "ok":
        return True
    _park_verify_failure(gh, issue, state, verify)
    gh.write_pinned_state(issue, state)
    return False


def _post_approval_comment(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _ReviewerRun,
) -> None:
    from orchestrator import workflow as _wf

    if reviewer_run.pr_number is None:
        return
    try:
        _wf._post_pr_comment(
            gh,
            int(reviewer_run.pr_number),
            state,
            f":white_check_mark: {config.REVIEW_AGENT} review approved.",
        )
    except Exception:
        _wf.log.exception(
            "issue=#%s could not post approval to PR #%s",
            issue.number,
            reviewer_run.pr_number,
        )


def _park_squash_failure(
    gh: GitHubClient, issue: Issue, state: PinnedState, error,
) -> None:
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        gh,
        issue,
        state,
        f"{config.HITL_MENTIONS} squash-on-approval failed "
        f"({error}); the original commits are still on the "
        "branch and the PR was not relabeled. Manual "
        "intervention needed (squash + force-push by hand, "
        "or set `SQUASH_ON_APPROVAL=off` and re-run the "
        "reviewer).",
        reason="squash_failed",
    )
    gh.write_pinned_state(issue, state)


def _squash_approved_work(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _ReviewerRun,
) -> Optional[int]:
    from orchestrator import workflow as _wf

    if not config.SQUASH_ON_APPROVAL:
        return 0
    squash_result = _wf._squash_and_force_push(
        spec,
        reviewer_run.wt,
        _wf._resolve_branch_name(state, spec, issue.number),
        issue,
    )
    if squash_result[0]:
        return squash_result[2]
    _park_squash_failure(gh, issue, state, squash_result[3])
    return None


def _finalize_validating_approval(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _ReviewerRun,
) -> None:
    """Finalize an approved review: verify gate, approval comment, optional
    squash, in_review handoff watermarks, then relabel to `documenting`.

    The verify gate is the first gate after the reviewer so an obviously-broken
    branch never reaches `in_review` (GitHub CI still runs against the PR for
    the human merging it). Default-empty `VERIFY_COMMANDS` short-circuits to
    "ok". A failed / timed-out command or a dirty tree left behind parks
    awaiting_human in `validating` with a stable `park_reason`. A failed
    squash / force-push also parks and STAYS in `validating` (no relabel) so
    the original commits remain on the branch for a human to adjudicate. On
    success the (possibly squashed) head routes through `documenting` for a
    final docs pass before in_review picks up; the watermarks, approval, and
    squash comment seeded here are preserved across the documenting hop.
    """
    if not _approved_work_verifies(gh, issue, state, reviewer_run):
        return
    _post_approval_comment(gh, issue, state, reviewer_run)
    squashed_count = _squash_approved_work(
        gh, spec, issue, state, reviewer_run,
    )
    if squashed_count is None:
        return
    _seed_in_review_handoff_watermarks(
        gh, issue, state, reviewer_run.pr_number, squashed_count,
    )
    gh.set_workflow_label(issue, WorkflowLabel.DOCUMENTING)
    gh.write_pinned_state(issue, state)


def _park_reviewer_no_verdict(
    gh: GitHubClient, issue: Issue, state: PinnedState, review
) -> None:
    """Park `validating` when the reviewer produced no VERDICT line.

    A silent crash (empty last message + non-zero exit -- codex-side error,
    network blip) is tagged transient (`reviewer_failed`) so the next tick
    re-spawns the reviewer instead of waking the dev on a human "Retry" comment;
    there is no review output the dev could act on, and
    `_resume_developer_on_human_reply` would otherwise hand the wrong agent a
    do-nothing prompt. A reviewer that emitted text but merely omitted the
    VERDICT line is left as `reviewer_no_verdict` for human adjudication, and
    stderr diagnostics are suppressed (the human is reading real model output).
    """
    from orchestrator import workflow as _wf

    raw = (review.last_message or "").strip() or "(reviewer produced no final message)"
    quoted = _wf._as_blockquote(raw)
    silent_crash = (
        not (review.last_message or "").strip() and review.exit_code != 0
    )
    diag = (
        ""
        if (review.last_message or "").strip()
        else _wf._format_stderr_diagnostics(review, "Reviewer")
    )
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} reviewer did not emit a VERDICT line; "
        f"manual adjudication needed.\n\n_Last reviewer message:_\n\n"
        f"{quoted}{diag}",
        reason=_REASON_REVIEWER_FAILED if silent_crash else "reviewer_no_verdict",
    )
    if silent_crash:
        state.set(_PARK_REASON, _REASON_REVIEWER_FAILED)
    _wf.log.warning(
        "issue=#%s reviewer emitted no VERDICT; exit_code=%d "
        "timed_out=%s stderr_tail=%r",
        issue.number, review.exit_code, review.timed_out,
        _wf._stderr_log_tail(review),
    )
    gh.write_pinned_state(issue, state)


def _post_reviewer_feedback(context: _RequestedChanges) -> None:
    from orchestrator import workflow as _wf

    reviewer_run = context.decision.run
    if reviewer_run.pr_number is None:
        return
    round_display = reviewer_run.round_n + 1
    feedback = context.decision.feedback
    try:
        reviewer_comment = _wf._post_pr_comment(
            context.gh,
            int(reviewer_run.pr_number),
            context.state,
            f":eyes: {config.REVIEW_AGENT} review "
            f"(round {round_display}/"
            f"{config.MAX_REVIEW_ROUNDS}) requested changes:\n\n"
            f"{feedback}",
        )
    except Exception:
        _wf.log.exception(
            "issue=#%s could not post review to PR #%s",
            context.issue.number,
            reviewer_run.pr_number,
        )
        return
    anchor_id = getattr(reviewer_comment, "id", None)
    if anchor_id is not None:
        context.state.set("pending_fix_reviewer_comment_id", int(anchor_id))


def _run_requested_fix(context: _RequestedChanges) -> _AwaitingDevAttempt:
    from orchestrator import workflow as _wf

    before_sha = _wf._head_sha(context.decision.run.wt)
    # The caller flipped the label validating -> fixing on the SAME `issue`
    # object; PyGithub does not refresh its cached `labels` after
    # `set_labels`, so pass `fixing` explicitly rather than let the resume
    # helper read the stale `validating` back off the issue and attribute this
    # developer run to the reviewer's stage.
    worktree, agent_result, paused = _wf._resume_dev_with_text(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        _wf._build_fix_prompt(context.decision.feedback),
        stage=WorkflowLabel.FIXING,
        pause_guard=True,
    )
    context.state.set("last_agent_action_at", _wf._now_iso())
    return _AwaitingDevAttempt(
        _DevFixRun(worktree, agent_result, before_sha), paused,
    )


def _finish_requested_fix(
    context: _RequestedChanges, attempt: _AwaitingDevAttempt,
) -> None:
    if attempt.paused:
        return
    pushed = _handle_dev_fix_result(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        attempt.run.worktree,
        attempt.run.agent_result,
        attempt.run.before_sha,
    )
    if not pushed:
        if not attempt.run.agent_result.interrupted:
            context.gh.write_pinned_state(context.issue, context.state)
        return
    context.state.set(_REVIEW_ROUND, context.decision.run.round_n + 1)
    context.state.set("pending_fix_reviewer_comment_id", None)
    context.gh.set_workflow_label(context.issue, WorkflowLabel.VALIDATING)
    context.gh.write_pinned_state(context.issue, context.state)


def _handle_validating_changes_requested(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    decision: _ReviewerDecision,
) -> None:
    """CHANGES_REQUESTED: post the reviewer feedback on the PR, flip to
    `fixing`, and resume the dev.

    The dev-fix subphase runs under the `fixing` label so the active job is
    observably "fixing reviewer-requested changes" rather than "validating"
    (which reads as reviewer/verify work only); `fixing` thereby extends to
    automated reviewer feedback in addition to its original in_review
    human-feedback duty. The label is flipped BEFORE the dev spawn so a crash
    inside the spawn still leaves the issue on `fixing` with stale
    awaiting_human=False, which the next tick's fixing handler treats as
    no-feedback and bounces back to `validating`. On a successful pushed fix we
    bump `review_round` and relabel to `validating`; on any park the issue stays
    on `fixing` and the fixing handler owns the awaiting-human rescan.
    `review_round` accounting, `MAX_REVIEW_ROUNDS`, dev-session pinning, and the
    final-docs handoff are unchanged -- only the visible label moves with the
    active work.

    The id of the reviewer-feedback PR comment is recorded in
    `pending_fix_reviewer_comment_id` so a session-failure park on this route
    (`agent_silent` / `agent_timeout`) is retryable by `/orchestrator continue`:
    the fixing handler's `_reconstruct_pending_fix_batch` replays that exact
    comment. `pending_fix_at` is deliberately NOT set (it discriminates the
    in_review route's review-round reset from this route's bump), so the anchor
    is a standalone key cleared on the pushed-fix exit here and inside
    `_clear_pending_fix_bookmarks`.
    """
    context = _RequestedChanges(gh, spec, issue, state, decision)
    _post_reviewer_feedback(context)
    gh.set_workflow_label(issue, WorkflowLabel.FIXING)
    gh.write_pinned_state(issue, state)
    _finish_requested_fix(context, _run_requested_fix(context))


def _park_review_cap(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    round_n: int,
) -> None:
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} review still has comments after "
        f"{round_n} round(s); manual intervention needed. To grant "
        "more rounds without losing the PR/worktree, reply with "
        "`/orchestrator add-review-rounds N` "
        "(N = additional rounds, e.g. `1`).",
        reason=_REASON_REVIEW_CAP,
    )
    # `_park_awaiting_human` clears `park_reason` by contract; the
    # awaiting-human branch needs this transient reason to route the
    # operator's `/orchestrator add-review-rounds` command.
    state.set(_PARK_REASON, _REASON_REVIEW_CAP)
    gh.write_pinned_state(issue, state)


def _run_reviewer_round(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    pr_number,
) -> Optional[_ReviewerRun]:
    from orchestrator import workflow as _wf

    round_n = int(state.get(_REVIEW_ROUND) or 0)
    if round_n >= config.MAX_REVIEW_ROUNDS:
        _park_review_cap(gh, issue, state, round_n)
        return None

    wt = _wf._ensure_worktree(
        spec, issue.number,
        branch=_wf._resolve_branch_name(state, spec, issue.number),
    )
    _, dev_backend_for_prompt, _, _ = _wf._read_dev_session(state)
    review_prompt = _wf._build_review_prompt(
        spec, issue, _wf._recent_comments_text(issue),
        config.default_repo_specs(), dev_backend_for_prompt,
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
    # Live pause: an operator applied `paused` / `backlog` while the reviewer
    # ran. Dispatch only saw the pre-run labels, so re-check a freshly fetched
    # issue and return WITHOUT folding usage, recording the review session,
    # parking, or relabeling -- durable GitHub state stays exactly as the prior
    # tick left it and the next tick re-spawns a fresh reviewer once the label
    # is removed. Nothing is stranded: the reviewer is read-only and spawns
    # fresh each round.
    if _wf._paused_during_agent_run(gh, issue):
        return None
    _wf._accumulate_issue_usage(state, review.usage)
    if review.session_id:
        state.set("last_review_session_id", review.session_id)
    state.set("last_review_at", _wf._now_iso())

    # Shutdown-sweep interruption: a reviewer run the orchestrator killed
    # mid-flight has no trustworthy verdict. Its empty output would otherwise
    # fall through to the `unknown` -> `reviewer_failed` park below and, on
    # the ensuing `write_pinned_state`, persist the usage counters just folded
    # above (and the session / `last_review_at` mutations). Ignore it and
    # return WITHOUT writing so those in-memory mutations are discarded and the
    # next process re-spawns the reviewer. Must precede the timeout/verdict
    # branches.
    if _wf._ignore_if_interrupted(issue, review):
        return None

    return _ReviewerRun(
        wt=wt,
        round_n=round_n,
        pr_number=pr_number,
        agent_result=review,
    )


def _dispatch_reviewer_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _ReviewerRun,
) -> None:
    from orchestrator import workflow as _wf

    review = reviewer_run.agent_result
    if review.timed_out:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} reviewer timed out after "
            f"{config.REVIEW_TIMEOUT}s; manual intervention needed.",
            reason=_REASON_REVIEWER_TIMEOUT,
        )
        # Tag as transient so the next tick re-spawns the reviewer instead
        # of waiting for a human comment that the timeout itself does not
        # produce.
        state.set(_PARK_REASON, _REASON_REVIEWER_TIMEOUT)
        gh.write_pinned_state(issue, state)
        return

    verdict, body = _wf._parse_review_verdict(review.last_message)
    decision = _ReviewerDecision(reviewer_run, verdict, body)
    gh.emit_event(
        "review_verdict",
        issue_number=issue.number,
        stage="validating",
        verdict=verdict,
        review_round=reviewer_run.round_n,
        pr_number=(
            None if reviewer_run.pr_number is None
            else int(reviewer_run.pr_number)
        ),
        session_id=review.session_id,
    )

    if decision.verdict == "approved":
        _finalize_validating_approval(
            gh, spec, issue, state, reviewer_run,
        )
        return

    if decision.verdict == "unknown":
        _park_reviewer_no_verdict(gh, issue, state, review)
        return

    # CHANGES_REQUESTED: post the reviewer feedback, flip to `fixing`, and
    # resume the dev. On a pushed fix the handler bumps `review_round` and
    # relabels back to `validating` so the reviewer re-evaluates the new head;
    # on any park the issue stays on `fixing` and the fixing handler owns the
    # awaiting-human rescan.
    _handle_validating_changes_requested(
        gh, spec, issue, state, decision,
    )


def _handle_validating(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if _finalize_validating_terminal(gh, spec, issue, state):
        return

    # User-content drift resume runs before the awaiting-human and reviewer
    # branches: a body edit mid-review must resume the dev on the new body
    # rather than re-review stale work. Returns True when it fully handled the
    # tick; a reviewer-side (`reviewer_timeout` / `reviewer_failed`) or
    # `review_cap` park defers to the awaiting-human branch below (that branch
    # owns the human's "retry" / `/orchestrator add-review-rounds` comment).
    if _resume_dev_on_validating_drift(gh, spec, issue, state):
        return

    # Awaiting-human path: human replied after a park (or a transient
    # condition self-resolved). The helper resumes the dev on their feedback,
    # recovers transient parks silently, or clears a reviewer-side / review-cap
    # park into a reviewer re-run. "return" -> the tick is fully handled;
    # "spawn_reviewer" -> fall through to the round-cap check and reviewer
    # spawn below.
    if state.get("awaiting_human"):
        if _handle_validating_awaiting_human(
            gh, spec, issue, state
        ) == _OUTCOME_RETURN:
            return

    reviewer_run = _run_reviewer_round(gh, spec, issue, state, pr_number)
    if reviewer_run is None:
        return

    _dispatch_reviewer_result(gh, spec, issue, state, reviewer_run)
