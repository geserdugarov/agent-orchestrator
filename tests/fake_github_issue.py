# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Issue, pinned-state, and event services for the fake GitHub client."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from orchestrator import analytics, config
from orchestrator.github import (
    PINNED_STATE_MARKER,
    PinnedState,
    WORKFLOW_LABELS,
    _write_event_record,
    build_event_record,
)
from orchestrator.state_machine import (
    WorkflowLabel,
    coerce_workflow_label,
    guard_transition,
)

from tests.fake_model_helpers import _has_closed_sweep_label
from tests.fake_github_state import _CommentHistory, _LabelHistory
from tests.fake_models import FakeComment, FakeIssue, FakeLabel


_STATE_CLOSED = "closed"


def _workflow_label(
    owner_or_issue,
    issue: Optional[FakeIssue] = None,
) -> Optional[WorkflowLabel]:
    target_issue = issue or owner_or_issue
    for label in target_issue.labels:
        if label.name in WORKFLOW_LABELS:
            return WorkflowLabel(label.name)
    return None


def _set_workflow_label(
    client,
    issue: FakeIssue,
    new_label: Optional[str],
) -> None:
    resolved_label = coerce_workflow_label(new_label) if new_label else None
    if resolved_label is not None:
        guard_transition(
            client.workflow_label(issue),
            resolved_label,
            config.WORKFLOW_TRANSITION_GUARD,
        )
    retained = [
        label
        for label in issue.labels
        if label.name not in WORKFLOW_LABELS
    ]
    if resolved_label:
        retained.append(FakeLabel(resolved_label))
    if not client._stale_label_cache:
        issue.labels = retained
    client.label_history.append((issue.number, resolved_label))
    if resolved_label:
        client.emit_event(
            "stage_enter",
            issue_number=issue.number,
            stage=resolved_label,
        )
        analytics.record_stage_enter(
            repo=client._repo_slug,
            issue=issue.number,
            stage=resolved_label,
        )


class _IssueHistoryView:
    @property
    def posted_comments(self) -> _CommentHistory:
        return self._issue_history._posted_comments

    @property
    def label_history(self) -> _LabelHistory:
        return self._issue_history._label_history

    @property
    def created_child_issues(self) -> list[FakeIssue]:
        return self._issue_history._created_child_issues

    @property
    def write_state_calls(self) -> int:
        return self._issue_history._write_state_calls


class _EventHistoryView:
    @property
    def recorded_events(self) -> list[dict]:
        return self._event_history._recorded_events


class _IssueService:
    def add_issue(self, issue: FakeIssue) -> None:
        self._issues[issue.number] = issue

    def list_pollable_issues(self) -> Iterable[FakeIssue]:
        pollable: list[FakeIssue] = []
        seen: set[int] = set()
        self._pollable_calls += 1
        for issue in self._issues.values():
            if issue.closed:
                continue
            seen.add(issue.number)
            pollable.append(issue)
        every = config.CLOSED_ISSUE_SWEEP_EVERY_N_TICKS
        if every > 1 and (self._pollable_calls - 1) % every != 0:
            return pollable
        for issue in self._issues.values():
            if (
                issue.closed
                and issue.number not in seen
                and _has_closed_sweep_label(issue)
            ):
                seen.add(issue.number)
                pollable.append(issue)
        return pollable

    def get_issue(self, number: int) -> FakeIssue:
        return self._issues[int(number)]

    def create_child_issue(
        self,
        *,
        title: str,
        body: str,
        parent_number: int,
        labels: list[str],
    ) -> FakeIssue:
        validated = [coerce_workflow_label(label) for label in labels]
        trimmed_body = (body or "").rstrip()
        full_body = f"{trimmed_body}\n\nParent: #{parent_number}"
        child = FakeIssue(
            number=next(self._next_issue_number),
            title=title,
            body=full_body,
            labels=[FakeLabel(label) for label in validated],
        )
        self._issues[child.number] = child
        self.created_child_issues.append(child)
        return child


class _WorkflowStateService:
    workflow_label = _workflow_label
    set_workflow_label = _set_workflow_label

    def seed_state(self, issue_number: int, **state_data: Any) -> None:
        self._pinned[issue_number] = PinnedState(
            comment_id=next(self._comment_id),
            data=dict(state_data),
        )

    def emit_event(
        self,
        event: str,
        *,
        issue_number: int,
        stage: Optional[str] = None,
        **extras: Any,
    ) -> None:
        record = build_event_record(
            repo=self._repo_slug,
            issue_number=issue_number,
            event=event,
            stage=stage,
            **extras,
        )
        self.recorded_events.append(record)
        _write_event_record(record)

    def read_pinned_state(self, issue: FakeIssue) -> PinnedState:
        existing = self._pinned.get(issue.number)
        if existing is None:
            return PinnedState()
        return PinnedState(
            comment_id=existing.comment_id,
            data=dict(existing.data),
        )

    def write_pinned_state(
        self,
        issue: FakeIssue,
        state: PinnedState,
    ) -> PinnedState:
        self._issue_history._write_state_calls += 1
        if state.comment_id is None:
            state.comment_id = next(self._comment_id)
            issue.comments.append(FakeComment(
                id=state.comment_id,
                body=f"{PINNED_STATE_MARKER} ... -->",
            ))
        self._pinned[issue.number] = PinnedState(
            comment_id=state.comment_id,
            data=dict(state.data),
        )
        return state

    def pinned_data(self, issue_number: int) -> dict[str, Any]:
        pinned_state = self._pinned.get(issue_number)
        if pinned_state is None:
            return {}
        return dict(pinned_state.data)


class _IssueCommentService:
    def comment(self, issue: FakeIssue, body: str) -> FakeComment:
        new_comment = FakeComment(id=next(self._comment_id), body=body)
        issue.comments.append(new_comment)
        self.posted_comments.append((issue.number, body))
        return new_comment

    def comments_after(
        self,
        issue: FakeIssue,
        after_id: Optional[int],
    ) -> list[FakeComment]:
        return [
            comment
            for comment in issue.comments
            if PINNED_STATE_MARKER not in (comment.body or "")
            and (after_id is None or comment.id > after_id)
        ]

    def latest_comment_id(self, issue: FakeIssue) -> Optional[int]:
        return max(
            (comment.id for comment in issue.comments),
            default=None,
        )
