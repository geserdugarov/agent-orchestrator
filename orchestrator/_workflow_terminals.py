# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow terminals."""
from __future__ import annotations

from orchestrator import _workflow_state as _state
from orchestrator import workflow as _owner

_ReviewTerminalContext = _owner._ReviewTerminalContext
Any = _owner.Any
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
config = _owner.config
dataclass = _owner.dataclass
_ISSUE_STATE_CLOSED = _state._ISSUE_STATE_CLOSED
_ISSUE_STATE_OPEN = _state._ISSUE_STATE_OPEN
_STATE_ATTR = _state._STATE_ATTR
log = _state.log


def _drain_review_terminal(context: _ReviewTerminalContext) -> bool:
    if context.pr is None:
        return False
    pr_status = context.gh.pr_state(context.pr)
    if pr_status == "merged":
        _owner._finalize_merged_pr(context, close_error="could not close after merge")
        return True
    if pr_status == _ISSUE_STATE_CLOSED:
        _owner._finalize_rejected_pr(context)
        return True
    if getattr(context.issue, _STATE_ATTR, _ISSUE_STATE_OPEN) == _ISSUE_STATE_CLOSED:
        _owner._finalize_closed_issue_with_open_pr(context)
        return True
    return False


def _drain_review_pr_terminals(
    gh: GitHubClient,
    *context_args,
    stage: str,
) -> bool:
    """Drain the three PR/issue terminal arcs shared by `_handle_in_review`,
    `_handle_fixing`, and `_handle_resolving_conflict`.

    Caller passes the already-fetched PR and its own `stage` label. Each
    stage owns its fetch-failure semantics: `in_review` and
    `resolving_conflict` let `gh.get_pr` exceptions propagate to
    `_process_issue`'s catch; `fixing` catches and bails with `pr=None`
    so the rest of its handler can short-circuit. Passing `pr=None` here
    is a no-op (returns False) so fixing's deferral arrives unchanged.

    Three arcs (mirrors the original inline code in each stage):

      1. `pr_state == "merged"`: stamp `merged_at`, flip to `done`,
         write state, emit `pr_merged` (`merge_method="external"`),
         close the issue if still open, and clean up the branch.
      2. `pr_state == "closed"` (unmerged): stamp
         `closed_without_merge_at`, flip to `rejected`, write state,
         emit `pr_closed_without_merge`, close the issue if still open,
         and clean up the branch.
      3. Issue is closed but PR is still open (the closed-issue sweep
         surfaced a human stop signal): stamp `closed_without_merge_at`,
         flip to `rejected`, write state. Deliberately no event emit
         (the PR is still open and may be reopened/salvaged) and no
         branch cleanup (the operator may want the open PR's history).

    Returns True when an arc fired (caller must return immediately).
    Returns False when none fired (caller continues with the same `pr`).
    """
    spec, issue, state, pr = context_args
    return _owner._drain_review_terminal(
        _ReviewTerminalContext(gh, spec, issue, state, pr, stage),
    )


@dataclass(frozen=True)
class _ClosedIssuePR:
    number: Optional[int]
    pr: Any = None
    defer: bool = False


def _closed_issue_pr(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> _ClosedIssuePR:
    raw_number = state.get("pr_number")
    if raw_number is None:
        return _ClosedIssuePR(number=None)
    number = int(raw_number)
    try:
        pr = gh.get_pr(number)
    except Exception:
        log.exception(
            "issue=#%s could not fetch PR #%s while finalizing a "
            "closed issue; deferring (next tick retries the "
            "merged-PR path)", issue.number, raw_number,
        )
        return _ClosedIssuePR(number=number, defer=True)
    return _ClosedIssuePR(
        number=number,
        pr=pr,
        defer=gh.pr_state(pr) == "merged",
    )


def _emit_closed_pr_rejection(context: _ReviewTerminalContext) -> None:
    context.gh.emit_event(
        "pr_closed_without_merge",
        issue_number=context.issue.number,
        stage=context.stage,
        pr_number=context.pr_number,
        sha=getattr(context.pr.head, "sha", None) or None,
        review_round=int(context.state.get("review_round") or 0),
        conflict_round=context.state.get("conflict_round"),
        retry_count=context.state.get("retry_count"),
    )
    _owner._cleanup_review_terminal(context)


def _finalize_if_issue_closed(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    """Flip a closed-but-not-merged issue to `rejected`.

    Pairs with `_finalize_if_pr_merged`: that helper drains the merged-PR
    arc, this one drains the closed-issue counterpart so closed issues
    yielded by the new `implementing` / `documenting` / `validating`
    sweep entries do NOT spawn the dev / docs / reviewer agent, push to
    the per-issue branch, or post on the now-closed issue thread.
    `_handle_in_review` / `_handle_fixing` carry equivalent guards
    inline via their PR-state arcs; callers in the new sweep stages
    invoke this helper right after `_finalize_if_pr_merged` so the
    merged case is drained first and only the rejected case lands here.

    Branch cleanup follows the in_review / fixing convention: only when
    the linked PR itself is also closed (a closed PR without merge is
    `pr_closed_without_merge`-emit territory and the branch is dead
    weight). An open PR with a manually-closed issue is left alone so
    the operator can salvage / reopen it; the orchestrator-owned branch
    and worktree stay until the PR closes.

    Returns True when the caller must NOT continue the handler this
    tick: the issue was finalized to `rejected`, OR the issue is closed
    but the linked PR state could not be confirmed yet (deferred to a
    later tick so a transient fetch failure cannot permanently mis-
    label a merged-PR issue, AND so the closed issue is not driven
    through normal dev / docs / reviewer work). Returns False only
    when the issue is still open and the handler should proceed.
    """
    if getattr(issue, _STATE_ATTR, _ISSUE_STATE_OPEN) != _ISSUE_STATE_CLOSED:
        return False
    linked_pr = _owner._closed_issue_pr(gh, issue, state)
    if linked_pr.defer:
        return True
    context = _ReviewTerminalContext(
        gh, spec, issue, state, linked_pr.pr, gh.workflow_label(issue),
    )
    _owner._finalize_closed_issue_with_open_pr(context)
    if linked_pr.pr is not None and gh.pr_state(linked_pr.pr) == _ISSUE_STATE_CLOSED:
        _owner._emit_closed_pr_rejection(context)
    return True
