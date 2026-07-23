# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``orchestrator.scheduler.IssueScheduler``.

Each test gates the workers with ``threading.Event`` so the in-flight
state under load is observable without depending on wall-clock timing.
Worker gates are held through ``_release_on_exit``, whose cleanup releases
them even when an assertion fails so shutdown cannot stall the suite.
"""

from __future__ import annotations

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


def _submit_then_signal(
    scheduler: IssueScheduler,
    worker,
    done: threading.Event,
) -> None:
    scheduler.submit(PRIMARY_REPO, 1, worker)
    done.set()


def _shutdown_then_signal(
    scheduler: IssueScheduler,
    done: threading.Event,
) -> None:
    scheduler.shutdown(wait=True)
    done.set()


def _release_after(delay: float, release: threading.Event) -> None:
    time.sleep(delay)
    release.set()


def _tracked_worker(
    scheduler: IssueScheduler,
    repo_slug: str,
    issue_number: int,
    signals: tuple[threading.Event, threading.Event, threading.Event],
) -> None:
    start, release, claimed_event = signals
    with scheduler.track_active(repo_slug, issue_number) as claimed:
        if claimed:
            claimed_event.set()
        start.set()
        release.wait(timeout=WORKER_TIMEOUT_SECONDS)
