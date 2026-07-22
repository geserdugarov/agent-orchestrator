# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pinned state and issue-comment watermark methods."""
from __future__ import annotations

import json
from typing import Optional

from github.Issue import Issue
from github.IssueComment import IssueComment

from orchestrator import _github_pinned
from orchestrator._github_issues import GitHubIssueMixin


class GitHubStateMixin(GitHubIssueMixin):
    """Durable pinned-state reads/writes and issue comment scans."""

    def read_pinned_state(self, issue: Issue) -> _github_pinned.PinnedState:
        """Return the first authenticated, state-only pinned comment."""
        trusted_login = getattr(self, "_bot_login", None)
        for issue_comment in issue.get_comments():
            pinned_state = _github_pinned.pinned_state_from_comment(
                issue_comment,
                trusted_login=trusted_login,
                issue_number=issue.number,
            )
            if pinned_state is not None:
                return pinned_state
        return _github_pinned.PinnedState()

    def write_pinned_state(
        self,
        issue: Issue,
        state: _github_pinned.PinnedState,
    ) -> _github_pinned.PinnedState:
        """Create or replace the issue's authoritative state-only comment."""
        body = _github_pinned.PINNED_STATE_TEMPLATE.format(
            payload=json.dumps(state.data, sort_keys=True),
        )
        if state.comment_id is None:
            created_comment = issue.create_comment(body)
            state.comment_id = created_comment.id
            return state
        for issue_comment in issue.get_comments():
            if issue_comment.id == state.comment_id:
                issue_comment.edit(body)
                return state
        created_comment = issue.create_comment(body)
        state.comment_id = created_comment.id
        return state

    def comments_after(
        self,
        issue: Issue,
        after_id: Optional[int],
    ) -> list[IssueComment]:
        """Return non-state issue comments newer than the watermark."""
        issue_comments: list[IssueComment] = []
        for issue_comment in issue.get_comments():
            if _github_pinned.PINNED_STATE_MARKER in (issue_comment.body or ""):
                continue
            if after_id is None or issue_comment.id > after_id:
                issue_comments.append(issue_comment)
        return issue_comments

    def latest_comment_id(self, issue: Issue) -> Optional[int]:
        """Return the largest issue-comment id, when any comment exists."""
        latest_id: Optional[int] = None
        for issue_comment in issue.get_comments():
            if latest_id is None or issue_comment.id > latest_id:
                latest_id = issue_comment.id
        return latest_id
