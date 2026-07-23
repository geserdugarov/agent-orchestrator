# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Composition root for the in-memory GitHub client."""
from __future__ import annotations

from itertools import count
from typing import Iterable

from tests import fake_github_issue as issue_service
from tests import fake_github_pr_service as pull_service
from tests import fake_github_pr_views as pull_views
from tests.fake_github_state import (
    _FakeEventHistory,
    _FakeIssueHistory,
    _FakePullHistory,
    _FakePullState,
)
from tests.fake_models import FakeIssue


class _IssueViews(
    issue_service._IssueHistoryView,
    issue_service._EventHistoryView,
):
    """Combine issue-facing history views."""


class _IssueServices(
    issue_service._IssueService,
    issue_service._WorkflowStateService,
    issue_service._IssueCommentService,
):
    """Combine issue operations behind one inheritance branch."""


class _IssueClient(_IssueViews, _IssueServices):
    """Compose the complete issue-side fake surface."""


class _PullViews(
    pull_views._PullHistoryView,
    pull_views._PullStateView,
):
    """Combine pull-request state and history views."""


class _PullServices(
    pull_service._PullCreationService,
    pull_service._PullStatusService,
    pull_service._PullFeedbackService,
):
    """Combine pull-request operations behind one inheritance branch."""


class _PullClient(_PullViews, _PullServices):
    """Compose the complete pull-request-side fake surface."""


class FakeGitHubClient(_IssueClient, _PullClient):
    """In-memory stand-in for orchestrator.github.GitHubClient."""

    def __init__(
        self,
        issues: Iterable[FakeIssue] = (),
        *,
        repo_slug: str = "geserdugarov/agent-orchestrator",
        stale_label_cache: bool = False,
    ) -> None:
        self._repo_slug = repo_slug
        self._stale_label_cache = stale_label_cache
        self._pollable_calls = 0
        self._issues = {issue.number: issue for issue in issues}
        self._pinned = {}
        self._comment_id = count(start=1000)
        self._pr_id = count(start=1)
        self._next_issue_number = count(
            start=max(self._issues, default=0) + 100,
        )
        self._issue_history = _FakeIssueHistory()
        self._event_history = _FakeEventHistory()
        self._pull_history = _FakePullHistory()
        self._pull_state = _FakePullState()
