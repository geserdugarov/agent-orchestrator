# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed storage owned by the in-memory GitHub client."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from tests.support.github.models import FakeComment, FakeIssue, FakePR


_LabelHistory = list[tuple[int, Optional[str]]]
_CommentHistory = list[tuple[int, str]]


@dataclass
class _FakeIssueHistory:
    _posted_comments: _CommentHistory = field(default_factory=list)
    _label_history: _LabelHistory = field(default_factory=list)
    _created_child_issues: list[FakeIssue] = field(default_factory=list)
    _write_state_calls: int = 0


@dataclass
class _FakePullHistory:
    _posted_pr_comments: _CommentHistory = field(default_factory=list)
    _opened_prs: list[FakePR] = field(default_factory=list)
    _edited_pr_bodies: _CommentHistory = field(default_factory=list)
    _merge_calls: list[tuple[int, str, str]] = field(default_factory=list)
    _deleted_remote_branches: list[str] = field(default_factory=list)


@dataclass
class _FakePullState:
    _existing_open_pr: dict[str, FakePR] = field(default_factory=dict)
    _pulls: dict[int, FakePR] = field(default_factory=dict)
    _merge_returns_ok: bool = True
    _delete_remote_branch_returns_ok: bool = True
    # PR numbers whose supersession GitHub refuses, which the real client
    # reports as False rather than raising -- the caller retries the whole
    # notice-and-close, which is idempotent on the second pass.
    _unsupersedable_prs: set[int] = field(default_factory=set)
    # PR numbers whose commit list GitHub refuses to serve, which the real
    # client reports as an unreadable lookup rather than as "does not carry
    # the commit".
    _unreadable_pr_commits: set[int] = field(default_factory=set)
    # Branches whose pull requests GitHub will not enumerate at all, which is
    # the same answer one level up: the candidate that would have matched is
    # one the walk never reaches.
    _unreadable_pr_lookups: set[str] = field(default_factory=set)


@dataclass
class _FakeEventHistory:
    _recorded_events: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class _IssueSeed:
    label: Optional[str] = None
    comments: Iterable[FakeComment] = ()
    title: str = "test issue"
    body: str = "test body"
    author: str = "geserdugarov"
