# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""In-memory issue and pull-request models used by workflow tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from tests.fake_model_helpers import _copy_issue_comments


_STATE_CLOSED = "closed"
_STATE_OPEN = "open"


@dataclass
class FakeUser:
    login: str = "human"
    type: str = "User"


@dataclass
class FakeComment:
    id: int
    body: str
    user: FakeUser = field(default_factory=FakeUser)
    created_at: Optional[datetime] = None


@dataclass
class FakeLabel:
    name: str


@dataclass
class FakeIssue:
    number: int
    title: str = "test issue"
    body: str = "test body"
    labels: list[FakeLabel] = field(default_factory=list)
    comments: list[FakeComment] = field(default_factory=list)
    closed: bool = False
    user: FakeUser = field(default_factory=lambda: FakeUser("geserdugarov"))

    get_comments = _copy_issue_comments

    @property
    def state(self) -> str:
        """Mirror the state exposed by PyGithub issues."""
        return _STATE_CLOSED if self.closed else _STATE_OPEN

    def edit(self, *, state: Optional[str] = None) -> None:
        if state == _STATE_CLOSED:
            self.closed = True


@dataclass
class FakePRRef:
    sha: str = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    ref: str = ""


@dataclass
class FakePRReview:
    """Stand-in for a PullRequestReview object."""

    id: int
    body: str
    state: str = "COMMENTED"
    user: FakeUser = field(default_factory=lambda: FakeUser("alice"))
    submitted_at: Optional[datetime] = None
    commit_id: str = ""


@dataclass
class FakePR:
    number: int
    head_branch: str = ""
    base_branch: str = "main"
    title: str = ""
    body: str = ""
    merged: bool = False
    state: str = _STATE_OPEN
    mergeable: Optional[bool] = True
    head: FakePRRef = field(default_factory=FakePRRef)
    approved: bool = False
    check_state: str = "none"
    user: FakeUser = field(default_factory=lambda: FakeUser("orchestrator"))
    labels: list[FakeLabel] = field(default_factory=list)
    issue_comments: list[FakeComment] = field(default_factory=list)
    review_comments: list[FakeComment] = field(default_factory=list)
    reviews: list[FakePRReview] = field(default_factory=list)
    approval_head_sha: Optional[str] = None
    changes_requested: bool = False
    changes_requested_head_sha: Optional[str] = None
