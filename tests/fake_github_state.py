# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed storage owned by the in-memory GitHub client."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from tests.fake_models import FakeComment, FakeIssue, FakePR


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
    _merge_calls: list[tuple[int, str, str]] = field(default_factory=list)
    _deleted_remote_branches: list[str] = field(default_factory=list)


@dataclass
class _FakePullState:
    _existing_open_pr: dict[str, FakePR] = field(default_factory=dict)
    _pulls: dict[int, FakePR] = field(default_factory=dict)
    _merge_returns_ok: bool = True
    _delete_remote_branch_returns_ok: bool = True


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
