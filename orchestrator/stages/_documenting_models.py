# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Documenting models."""
from __future__ import annotations

from orchestrator.stages import _documenting_state as _state
from orchestrator.stages import documenting as _owner

AgentResult = _owner.AgentResult
Any = _owner.Any
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
dataclass = _owner.dataclass
_PARK_REASON = _state._PARK_REASON


@dataclass(frozen=True)
class _DocumentingContext:
    """The per-tick `documenting` invocation handles plus the resolved
    `branch` and pinned `pr_number`, bundled so the drift-unwind,
    worktree-prep, docs run, and disposition helpers thread them as a single
    value instead of up to six positional arguments (mirrors fixing's
    `_FixingContext`). `branch` and `pr_number` are tick-invariant once
    `_handle_documenting`'s missing-`pr_number` guard has passed, so every
    consumer downstream of the guards reads them off the context.
    """
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    branch: str
    pr_number: Any


@dataclass(frozen=True)
class _DocumentingRun:
    """The outcome of one documenting attempt: the worktree the pass ran in,
    the agent result, the HEAD before the run, whether it was the
    recovered-commit shortcut (no agent spawned), whether an operator paused
    mid-run, and the worktree's ahead count vs. `<remote>/<branch>`. `ahead`
    is threaded to the disposition so a no-change verdict over a recovered
    commit still pushes it.
    """
    worktree: Any
    agent_result: AgentResult
    before_sha: str
    recovered: bool
    paused: bool
    ahead: int


def _park_documenting(
    ctx: _DocumentingContext, message: str, reason: str,
) -> None:
    """Park the docs pass awaiting a human and re-stamp the durable
    `park_reason`.

    `_park_awaiting_human` clears `park_reason` by contract; re-set the
    durable tag so future ticks / dashboards can branch on it -- documenting's
    awaiting-human resume also reads it to distinguish stale park flags after
    a relabel. Writes pinned state; the caller returns unconditionally.
    """
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        ctx.gh, ctx.issue, ctx.state, message, reason=reason,
    )
    ctx.state.set(_PARK_REASON, reason)
    ctx.gh.write_pinned_state(ctx.issue, ctx.state)


def _ratchet_in_review_watermark_for_final_docs(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Ratchet `pr_last_comment_id` past issue-thread comments the docs
    pass already consumed during the final-docs hop.

    During documenting's awaiting-human resume the handler advances
    `last_action_comment_id` past the human reply it fed into the
    `_build_documentation_prompt` resume. The final-docs handoff then
    relabels to `in_review`, which scans `comments_after(issue,
    pr_last_comment_id)` and falls back to `last_action_comment_id`
    only when `pr_last_comment_id is None`. Without this ratchet a
    `pr_last_comment_id` validating seeded BEFORE the human's reply
    keeps the older value, the consumed reply replays as fresh PR
    feedback, and in_review bounces the issue to `fixing` over work
    the dev has already addressed.

    Reuse `_latest_pr_comment_ids` (the same seed-walk validating uses
    at its approval handoff) so a PR-conversation comment with id
    between the prior `pr_last_comment_id` and the consumed-through
    threshold is NOT swallowed -- the walk stops at the first unread
    non-orchestrator comment on either surface. `consumed_through` is
    applied to the issue thread only inside the walk, which is what
    keeps PR-conversation feedback visible to in_review's
    fresh-feedback scan. Ratchets via `max` so a previous in_review
    tick's higher watermark is never regressed.

    A PR fetch failure is treated as best-effort: log and skip, so the
    docs handoff itself still advances. In the worst case in_review
    will route to `fixing` and the rescan there is debounced and
    correct on its own.
    """
    from orchestrator import workflow as _wf

    pr_number = state.get("pr_number")
    if pr_number is None:
        return
    try:
        pr = gh.get_pr(int(pr_number))
    except Exception as error:
        _wf.log.warning(
            "issue=#%s could not fetch PR #%s to ratchet "
            "`pr_last_comment_id` on the final-docs handoff: %s",
            issue.number, pr_number, error,
        )
        return

    candidate, _ = _wf._latest_pr_comment_ids(gh, issue, pr, state)
    prev_wm = state.get("pr_last_comment_id")
    if isinstance(prev_wm, int):
        candidate = (
            prev_wm if candidate is None
            else max(candidate, prev_wm)
        )
    if candidate is None:
        return
    state.set("pr_last_comment_id", candidate)


def _advance_after_docs_push(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Route the issue forward after a successful docs push.

    Advance to `in_review` -- the approval comment, squash comment, and
    PR watermarks set by validating remain on state untouched, with the
    in-review issue-comment watermark ratcheted past anything the
    awaiting-human resume already consumed.
    """
    _owner._ratchet_in_review_watermark_for_final_docs(gh, issue, state)
    gh.set_workflow_label(issue, WorkflowLabel.IN_REVIEW)


def _advance_after_docs_no_change(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Route the issue forward after a clean no-change docs verdict.

    No commit landed, so the PR head is unchanged. Ratchet the in-review
    issue-comment watermark past any issue-thread reply the
    awaiting-human resume already consumed, and advance to `in_review`.
    """
    _owner._ratchet_in_review_watermark_for_final_docs(gh, issue, state)
    gh.set_workflow_label(issue, WorkflowLabel.IN_REVIEW)
