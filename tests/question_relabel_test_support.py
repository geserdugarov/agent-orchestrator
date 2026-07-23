# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tests.fakes import FakeGitHubClient, FakeIssue, make_issue

LABEL_IMPLEMENTING = "implementing"


@dataclass(frozen=True)
class RelabelCase:
    issue_number: int
    park_reason: str
    watermark: int
    worktree: Path
    create_worktree: bool = True


@dataclass(frozen=True)
class RelabelFixture:
    github: FakeGitHubClient
    issue: FakeIssue
    worktree: Path


def _seed_relabel(relabel_case: RelabelCase) -> RelabelFixture:
    github = FakeGitHubClient()
    issue = make_issue(
        relabel_case.issue_number,
        label=LABEL_IMPLEMENTING,
    )
    github.add_issue(issue)
    github.seed_state(
        issue.number,
        awaiting_human=True,
        park_reason=relabel_case.park_reason,
        last_action_comment_id=relabel_case.watermark,
    )
    if relabel_case.create_worktree:
        relabel_case.worktree.mkdir(parents=True, exist_ok=True)
    return RelabelFixture(
        github=github,
        issue=issue,
        worktree=relabel_case.worktree,
    )
