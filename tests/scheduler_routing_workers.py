# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import threading
import time
from pathlib import Path


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


def _wait_for_first_started(
    starts: dict[int, threading.Event],
    *,
    timeout: float = EVENT_TIMEOUT_SECONDS,
) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for issue_number, started in starts.items():
            if started.is_set():
                return issue_number
        time.sleep(POLL_INTERVAL_SECONDS)
    return None


def _record_current_thread(thread_ids: list[int], _gh, _spec, _issue) -> None:
    thread_ids.append(threading.get_ident())


def _wait_for_log(
    log_capture,
    *fragments: str,
    timeout: float = EVENT_TIMEOUT_SECONDS,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for message in log_capture.output:
            if all(fragment in message for fragment in fragments):
                return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


class _IssueProcessor:
    def __init__(self, issue_numbers: tuple[int, ...], *, blocking: bool = True):
        self.starts = {issue_number: threading.Event() for issue_number in issue_numbers}
        self.releases = {issue_number: threading.Event() for issue_number in issue_numbers}
        self.processed: list[int] = []
        self._blocking = blocking
        self._lock = threading.Lock()

    def __call__(self, _gh, _spec, issue) -> None:
        with self._lock:
            self.processed.append(issue.number)
        start = self.starts.get(issue.number)
        if start is not None:
            start.set()
        if self._blocking:
            release = self.releases.get(issue.number)
            if release is not None:
                release.wait(timeout=WORKER_TIMEOUT_SECONDS)

    def release_all(self) -> None:
        for release in self.releases.values():
            release.set()

    def processed_snapshot(self) -> list[int]:
        with self._lock:
            return list(self.processed)


class _GatedWorker:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> None:
        self.started.set()
        self.release.wait(timeout=WORKER_TIMEOUT_SECONDS)


class _SequentialIssueProcessor(_IssueProcessor):
    def __init__(self, issue_numbers: tuple[int, ...]):
        super().__init__(issue_numbers)
        self.maximum_in_flight = 0
        self._in_flight = 0

    def __call__(self, gh, spec, issue) -> None:
        with self._lock:
            self._in_flight += 1
            self.maximum_in_flight = max(
                self.maximum_in_flight,
                self._in_flight,
            )
        try:
            super().__call__(gh, spec, issue)
        except BaseException:
            self._leave()
            raise
        else:
            self._leave()

    def _leave(self) -> None:
        with self._lock:
            self._in_flight -= 1


class _BarrierIssueProcessor:
    def __init__(self, parties: int):
        self._barrier = threading.Barrier(
            parties,
            timeout=WORKER_TIMEOUT_SECONDS,
        )
        self._processed: list[int] = []
        self._lock = threading.Lock()

    def __call__(self, _gh, _spec, issue) -> None:
        self._barrier.wait()
        with self._lock:
            self._processed.append(issue.number)

    def processed_snapshot(self) -> list[int]:
        with self._lock:
            return list(self._processed)
