# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Core fixtures for workflow tick concurrency tests."""
from __future__ import annotations

import contextlib
import threading
import unittest
from pathlib import Path

from orchestrator import config

from tests import fakes as _fakes
from tests import workflow_helpers as _helpers


KEY_AWAITING_HUMAN = _helpers.KEY_AWAITING_HUMAN
KEY_PARENT_NUMBER = _helpers.KEY_PARENT_NUMBER
KEY_PARK_REASON = _helpers.KEY_PARK_REASON
LABEL_BLOCKED = _helpers.LABEL_BLOCKED
LABEL_DECOMPOSING = _helpers.LABEL_DECOMPOSING
LABEL_IMPLEMENTING = _helpers.LABEL_IMPLEMENTING
LABEL_READY = _helpers.LABEL_READY
LABEL_UMBRELLA = _helpers.LABEL_UMBRELLA
_TEST_SPEC = _helpers._TEST_SPEC

FakeGitHubClient = _fakes.FakeGitHubClient
make_issue = _fakes.make_issue

PROCESS_ISSUE = "_process_issue"
REFRESH_BASE = "_refresh_base_and_worktrees"

_WORKER_ISSUE_NUMBERS = (1, 2, 3)
_ORIGINAL_WORKFLOW_LABEL = FakeGitHubClient.workflow_label
_DEFAULT_PROBE_DELAY_SECONDS = 0.01
_SYNC_TIMEOUT_SECONDS = 5.0
_FAMILY_OVERLAP_DELAY_SECONDS = 0.05
_FAMILY_POLL_DELAY_SECONDS = 0.01
_SERIAL_PROBE_DELAY_SECONDS = 0.02
_FANOUT_ISSUE_NUMBER = 99
_FAMILY_CHILD_ISSUE_NUMBER = 20


def _spec(parallel_limit: int) -> config.RepoSpec:
    return config.RepoSpec(
        slug="acme/widget",
        target_root=Path("/tmp/orchestrator-test-target-root"),
        base_branch="main",
        parallel_limit=parallel_limit,
    )


def _seed_issues(
    client: FakeGitHubClient,
    numbers,
    *,
    label: str = LABEL_IMPLEMENTING,
) -> None:
    for issue_number in numbers:
        client.add_issue(make_issue(issue_number, label=label))


def _poll_then_raise(gh: FakeGitHubClient):
    yield gh.get_issue(1)
    yield gh.get_issue(2)
    raise RuntimeError("simulated pagination failure")


@contextlib.contextmanager
def _running_thread(target, cleanup):
    thread = threading.Thread(target=target)
    thread.start()
    try:
        yield
    finally:
        cleanup(thread)


class _TrackingGitHubClient(FakeGitHubClient):
    def __init__(
        self,
        get_issue_calls: list[tuple[int, int]],
        calls_lock: threading.Lock,
    ) -> None:
        super().__init__()
        self._get_issue_calls = get_issue_calls
        self._calls_lock = calls_lock

    def get_issue(self, number: int):
        with self._calls_lock:
            self._get_issue_calls.append((number, id(self)))
        return super().get_issue(number)


class _WorkerClientScenario:
    def __init__(self) -> None:
        self.get_issue_calls: list[tuple[int, int]] = []
        self.process_calls: list[tuple[int, int]] = []
        self._calls_lock = threading.Lock()
        self.parent = _TrackingGitHubClient(
            self.get_issue_calls,
            self._calls_lock,
        )
        self.cloned_clients: list[FakeGitHubClient] = []
        _seed_issues(self.parent, _WORKER_ISSUE_NUMBERS)

    def clone_client(self) -> FakeGitHubClient:
        twin = _TrackingGitHubClient(self.get_issue_calls, self._calls_lock)
        _seed_issues(twin, _WORKER_ISSUE_NUMBERS)
        with self._calls_lock:
            self.cloned_clients.append(twin)
        return twin

    def process_issue(self, worker_client, _spec, issue) -> None:
        with self._calls_lock:
            self.process_calls.append((issue.number, id(worker_client)))

    def assert_distinct_worker_clients(self, case: unittest.TestCase) -> None:
        worker_ids = {id(client) for client in self.cloned_clients}
        case.assertEqual(len(self.cloned_clients), len(_WORKER_ISSUE_NUMBERS))
        case.assertEqual(len(worker_ids), len(_WORKER_ISSUE_NUMBERS))
        case.assertNotIn(id(self.parent), worker_ids)

    def assert_worker_refetches(self, case: unittest.TestCase) -> None:
        parent_id = id(self.parent)
        fetched_by_issue = dict(self.get_issue_calls)
        case.assertEqual(len(self.get_issue_calls), len(_WORKER_ISSUE_NUMBERS))
        case.assertNotIn(parent_id, fetched_by_issue.values())
        case.assertEqual(
            dict(self.process_calls),
            fetched_by_issue,
        )
        case.assertEqual(
            sorted(fetched_by_issue),
            list(_WORKER_ISSUE_NUMBERS),
        )
