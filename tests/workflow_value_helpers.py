# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Small value builders shared by workflow tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.github import PinnedState

from tests.fakes import FakeGitHubClient
from tests.workflow_repo_values import (
    TEST_REPO_SLUG,
    _FAKE_WT,
)


def _iso_hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds",
    )


def _manifest(payload: str) -> str:
    return f"```orchestrator-manifest\n{payload}\n```"


def _issue_branch(
    issue_number: int,
    slug: str = TEST_REPO_SLUG,
) -> str:
    return f"orchestrator/{slug.replace('/', '__')}/issue-{issue_number}"


def _fake_worktree(*_args, **_kwargs) -> Path:
    return _FAKE_WT


def _state_with_pr_number(
    github: FakeGitHubClient,
    issue_number: int,
    pr_number: int,
    **extra,
) -> PinnedState:
    seed = {"pr_number": pr_number, **extra}
    github.seed_state(issue_number, **seed)
    return PinnedState(comment_id=None, data=dict(seed))


def _analytics_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
