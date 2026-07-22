# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow terminal actions."""
from __future__ import annotations

from orchestrator import _workflow_state as _state
from orchestrator import workflow as _owner

Any = _owner.Any
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
dataclass = _owner.dataclass
_ISSUE_STATE_CLOSED = _state._ISSUE_STATE_CLOSED
_ISSUE_STATE_OPEN = _state._ISSUE_STATE_OPEN
_STATE_ATTR = _state._STATE_ATTR
log = _state.log


def _finalize_if_pr_merged(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    """Flip the issue to `done` when its linked PR has already merged.

    Mirrors the terminal-merge arc in `_handle_in_review` / `_handle_fixing`
    / `_handle_resolving_conflict` so the same finalize path can fire from
    any stage. Used by handlers that previously had no merged-PR check
    (`_handle_implementing`, `_handle_documenting`, `_handle_validating`)
    and by the umbrella / blocked aggregation when a child PR was merged
    externally but the child's workflow label was never advanced past the
    in-flight stage -- the umbrella's all-`done` aggregation would
    otherwise wait forever for that stale child.

    Returns True when the helper finalized the issue (caller must return
    immediately); False when there is nothing to do (no `pr_number`, PR
    fetch failed, or PR is not merged).
    """
    pr_number = state.get("pr_number")
    if pr_number is None:
        return False
    try:
        pr = gh.get_pr(int(pr_number))
    except Exception:
        log.exception(
            "issue=#%s could not fetch PR #%s while checking for "
            "external merge; leaving alone", issue.number, pr_number,
        )
        return False
    if gh.pr_state(pr) != "merged":
        return False
    _owner._finalize_merged_pr(
        _ReviewTerminalContext(
            gh=gh,
            spec=spec,
            issue=issue,
            state=state,
            pr=pr,
            stage=gh.workflow_label(issue),
        ),
        close_error="could not close after detecting external merge",
        close_if_open_only=True,
    )
    return True


@dataclass(frozen=True)
class _ReviewTerminalContext:
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    pr: Any
    stage: Optional[str]

    @property
    def pr_number(self) -> int:
        return int(self.state.get("pr_number"))

    @property
    def conflict_round(self):
        conflict_round = self.state.get("conflict_round")
        if self.stage == "resolving_conflict":
            return int(conflict_round or 0)
        return conflict_round


def _close_terminal_issue(
    context: _ReviewTerminalContext, error_message: str,
) -> None:
    try:
        context.issue.edit(state=_ISSUE_STATE_CLOSED)
    except Exception:
        log.exception(
            "issue=#%s %s", context.issue.number, error_message,
        )


def _cleanup_review_terminal(context: _ReviewTerminalContext) -> None:
    _owner._cleanup_terminal_branch(
        context.gh,
        context.spec,
        context.issue.number,
        branch=_owner._resolve_branch_name(
            context.state, context.spec, context.issue.number,
        ),
    )


def _finalize_merged_pr(
    context: _ReviewTerminalContext,
    *,
    close_error: str,
    close_if_open_only: bool = False,
) -> None:
    context.state.set("merged_at", _owner._now_iso())
    context.gh.set_workflow_label(context.issue, WorkflowLabel.DONE)
    _owner._post_issue_usage_verdict(context.gh, context.issue, context.state)
    context.gh.write_pinned_state(context.issue, context.state)
    context.gh.emit_event(
        "pr_merged",
        issue_number=context.issue.number,
        stage=context.stage,
        pr_number=context.pr_number,
        sha=getattr(context.pr.head, "sha", None) or None,
        merge_method="external",
        review_round=int(context.state.get("review_round") or 0),
        conflict_round=context.conflict_round,
        retry_count=context.state.get("retry_count"),
    )
    if (
        not close_if_open_only
        or getattr(context.issue, _STATE_ATTR, _ISSUE_STATE_OPEN) != _ISSUE_STATE_CLOSED
    ):
        _owner._close_terminal_issue(context, close_error)
    _owner._cleanup_review_terminal(context)


def _finalize_rejected_pr(context: _ReviewTerminalContext) -> None:
    context.state.set("closed_without_merge_at", _owner._now_iso())
    context.gh.set_workflow_label(context.issue, WorkflowLabel.REJECTED)
    _owner._post_issue_usage_verdict(context.gh, context.issue, context.state)
    context.gh.write_pinned_state(context.issue, context.state)
    context.gh.emit_event(
        "pr_closed_without_merge",
        issue_number=context.issue.number,
        stage=context.stage,
        pr_number=context.pr_number,
        sha=getattr(context.pr.head, "sha", None) or None,
        review_round=int(context.state.get("review_round") or 0),
        conflict_round=context.conflict_round,
        retry_count=context.state.get("retry_count"),
    )
    _owner._close_terminal_issue(context, "could not close after reject")
    _owner._cleanup_review_terminal(context)


def _finalize_closed_issue_with_open_pr(context: _ReviewTerminalContext) -> None:
    context.state.set("closed_without_merge_at", _owner._now_iso())
    context.gh.set_workflow_label(context.issue, WorkflowLabel.REJECTED)
    _owner._post_issue_usage_verdict(context.gh, context.issue, context.state)
    context.gh.write_pinned_state(context.issue, context.state)
