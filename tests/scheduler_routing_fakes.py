# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import threading
from pathlib import Path


from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    LABEL_IMPLEMENTING,
)

REPO_SLUG = "acme/widget"
TARGET_ROOT = Path("/tmp/orchestrator-test-target-root")
PROCESS_ISSUE = "_process_issue"
REFRESH_BASE = "_refresh_base_and_worktrees"
FANOUT_START_TIMEOUT_MESSAGE = "implementing fanout #1 did not start"
POLL_INTERVAL_SECONDS = 0.01
EVENT_TIMEOUT_SECONDS = 2.0
WORKER_TIMEOUT_SECONDS = 5.0
DEFERRED_ISSUE_NUMBERS = (10, 11, 12)
FAMILY_ISSUE_NUMBER = 42
RELABELLED_FANOUT_ISSUE_NUMBER = 50


class _WorkerClientFactory:
    def __init__(self) -> None:
        self.clients: list[FakeGitHubClient] = []
        self._lock = threading.Lock()

    def __call__(self) -> FakeGitHubClient:
        client = FakeGitHubClient()
        client.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))
        with self._lock:
            self.clients.append(client)
        return client


class _FakeWorktreeDir:
    name = "issue-7"

    def is_dir(self) -> bool:
        return True


class _FakeWorktreeRoot:
    def exists(self) -> bool:
        return True

    def iterdir(self) -> list[_FakeWorktreeDir]:
        return [_FakeWorktreeDir()]


class _PyGithubIssue:
    def __init__(self, state: str):
        self.state = state
