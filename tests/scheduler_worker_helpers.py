# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``orchestrator.scheduler.IssueScheduler``.

Each test gates the workers with ``threading.Event`` so the in-flight
state under load is observable without depending on wall-clock timing.
Worker gates are held through ``_release_on_exit``, whose cleanup releases
them even when an assertion fails so shutdown cannot stall the suite.
"""

from __future__ import annotations

import contextlib
import threading
import time

from orchestrator.scheduler import IssueScheduler

PRIMARY_REPO = "owner/repo"
OTHER_REPO = "owner/other"
REPO_A = "owner/a"
REPO_B = "owner/b"
SCHEDULER_LOGGER = "orchestrator.scheduler"
WORKER_FAILURE = "worker exploded"
FORBIDDEN_WORKER_MESSAGE = "must not run"
POLL_INTERVAL_SECONDS = 0.01
EVENT_TIMEOUT_SECONDS = 2.0
WORKER_TIMEOUT_SECONDS = 5.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
BRIEF_TIMEOUT_SECONDS = 0.05
CAP_REJECTION_ISSUE_NUMBER = 99
FIRST_FAMILY_ISSUE_NUMBER = 100
SECOND_FAMILY_ISSUE_NUMBER = 101
OTHER_FAMILY_ISSUE_NUMBER = 102
FANOUT_ISSUE_NUMBER = 200
COMPLETED_FAMILY_ISSUE_NUMBER = 50
FOLLOW_UP_FAMILY_ISSUE_NUMBER = 51
TRACKED_ISSUE_NUMBER = 42


@contextlib.contextmanager
def _release_on_exit(*events: threading.Event):
    try:
        yield
    finally:
        for event in events:
            event.set()


def _wait_until_inactive(
    scheduler: IssueScheduler,
    repo_slug: str,
    issue_number: int,
    *,
    timeout: float = EVENT_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while scheduler.is_active(repo_slug, issue_number) and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)


def _worker(start: threading.Event, release: threading.Event) -> None:
    """Standard worker body: signal that the thread has started, then
    block until the test releases it. Used by every concurrency test."""
    start.set()
    release.wait(timeout=WORKER_TIMEOUT_SECONDS)


def _finishing_worker(
    start: threading.Event,
    release: threading.Event,
    finished: threading.Event,
) -> None:
    _worker(start, release)
    finished.set()


def _failing_worker(message: str = WORKER_FAILURE) -> None:
    raise RuntimeError(message)


def _signaling_failure(done: threading.Event, message: str) -> None:
    done.set()
    raise RuntimeError(message)


def _gated_failure(
    start: threading.Event,
    release: threading.Event,
    message: str,
) -> None:
    _worker(start, release)
    raise RuntimeError(message)
