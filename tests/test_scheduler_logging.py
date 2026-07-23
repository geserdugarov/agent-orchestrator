# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``orchestrator.scheduler.IssueScheduler``.

Each test gates the workers with ``threading.Event`` so the in-flight
state under load is observable without depending on wall-clock timing.
Worker gates are held through ``_release_on_exit``, whose cleanup releases
them even when an assertion fails so shutdown cannot stall the suite.
"""

from __future__ import annotations

import logging
import threading
import unittest

from orchestrator.scheduler import IssueScheduler

from tests.scheduler_log_helpers import _contains_log
from tests.scheduler_worker_helpers import (
    _release_on_exit,
    _worker,
)

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


class SubmitSkipLoggingTest(unittest.TestCase):
    """Every skip path in `submit` emits a `scheduler skip ...` log line
    so an operator can correlate "issue not advancing" with the precise
    reason (closed / duplicate / cap / family slot). The duplicate-active
    case is the common one for a long-running worker and uses DEBUG to
    avoid spamming; the rarer reasons use INFO.
    """

    def test_family_slot_skip_logs_at_info(self) -> None:
        sched = IssueScheduler(global_cap=10, per_repo_cap=10)
        self.addCleanup(sched.shutdown)
        start = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    FIRST_FAMILY_ISSUE_NUMBER,
                    lambda: _worker(start, release),
                    family=True,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            with self.assertLogs(
                SCHEDULER_LOGGER,
                level=logging.INFO,
            ) as logs:
                self.assertFalse(
                    sched.submit(
                        PRIMARY_REPO,
                        SECOND_FAMILY_ISSUE_NUMBER,
                        lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                        family=True,
                    )
                )
                log_output = logs.output
            self.assertTrue(
                _contains_log(
                    log_output,
                    "scheduler skip",
                    "family_slot_held",
                    "#101",
                ),
                log_output,
            )

    def test_per_repo_cap_skip_logs_at_info(self) -> None:
        sched = IssueScheduler(global_cap=10, per_repo_cap=5)
        self.addCleanup(sched.shutdown)
        start = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    1,
                    lambda: _worker(start, release),
                    per_repo_cap=1,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            with self.assertLogs(
                SCHEDULER_LOGGER,
                level=logging.INFO,
            ) as logs:
                self.assertFalse(
                    sched.submit(
                        PRIMARY_REPO,
                        2,
                        lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                        per_repo_cap=1,
                    )
                )
                log_output = logs.output
            self.assertTrue(
                _contains_log(log_output, "scheduler skip", "per_repo_cap"),
                log_output,
            )

    def test_duplicate_active_skip_logs_at_debug(self) -> None:
        # The duplicate-active path is the routine case while a
        # long-running worker straddles ticks. DEBUG-level log keeps the
        # normal busy repo from spamming the operator log.
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        start = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    1,
                    lambda: _worker(start, release),
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            with self.assertLogs(
                SCHEDULER_LOGGER,
                level=logging.DEBUG,
            ) as logs:
                self.assertFalse(
                    sched.submit(
                        PRIMARY_REPO,
                        1,
                        lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                    )
                )
                log_output = logs.output
            self.assertTrue(
                _contains_log(log_output, "scheduler skip", "duplicate_active"),
                log_output,
            )
