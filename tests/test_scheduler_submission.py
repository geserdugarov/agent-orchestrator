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
from functools import partial

from orchestrator.scheduler import IssueScheduler

from tests.scheduler_worker_helpers import (
    _release_on_exit,
    _signaling_failure,
    _wait_until_inactive,
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


class DuplicateActiveIssueSkipTest(unittest.TestCase):
    def test_same_key_skipped_while_first_in_flight(self) -> None:
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        start = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(sched.submit(PRIMARY_REPO, 1, lambda: _worker(start, release)))
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            # Same (repo_slug, issue_number) is rejected even though both
            # global and per-repo caps still have spare slots.
            self.assertFalse(sched.submit(PRIMARY_REPO, 1, lambda: self.fail(FORBIDDEN_WORKER_MESSAGE)))
            self.assertEqual(sched.active_count(), 1)
            self.assertTrue(sched.is_active(PRIMARY_REPO, 1))

            # Same issue NUMBER on a different repo slug is a different
            # key and IS accepted -- the in-flight set is keyed on the
            # pair, not the number alone.
            start_b = threading.Event()
            self.assertTrue(sched.submit(OTHER_REPO, 1, lambda: _worker(start_b, release)))
            self.assertTrue(start_b.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(), 2)


class CompletionClearingTest(unittest.TestCase):
    def test_completion_clears_marker_for_resubmit(self) -> None:
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        done = threading.Event()

        self.assertTrue(sched.submit(PRIMARY_REPO, 7, done.set))
        self.assertTrue(done.wait(timeout=EVENT_TIMEOUT_SECONDS))

        # Wait for the done-callback to clear the marker. The callback
        # runs on a background thread, so poll briefly to avoid a race
        # between worker exit and marker clear.
        _wait_until_inactive(sched, PRIMARY_REPO, 7)
        self.assertFalse(sched.is_active(PRIMARY_REPO, 7))
        self.assertEqual(sched.active_count(), 0)
        self.assertEqual(sched.active_count(PRIMARY_REPO), 0)

        # Now a fresh submit for the same key succeeds.
        start = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(sched.submit(PRIMARY_REPO, 7, lambda: _worker(start, release)))
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

    def test_completion_logs_worker_failure_via_reap(self) -> None:
        sched = IssueScheduler(global_cap=2, per_repo_cap=2)
        self.addCleanup(sched.shutdown)
        done = threading.Event()

        self.assertTrue(
            sched.submit(
                PRIMARY_REPO,
                9,
                partial(_signaling_failure, done, WORKER_FAILURE),
            )
        )
        self.assertTrue(done.wait(timeout=EVENT_TIMEOUT_SECONDS))
        # The marker must clear regardless of the exception: a failed
        # worker still hands its slot back.
        _wait_until_inactive(sched, PRIMARY_REPO, 9)
        self.assertFalse(sched.is_active(PRIMARY_REPO, 9))

        with self.assertLogs(SCHEDULER_LOGGER, level=logging.ERROR) as logs:
            count = sched.reap()
            log_output = logs.output
        self.assertGreaterEqual(count, 1)
        self.assertTrue(
            any(WORKER_FAILURE in message for message in log_output),
            log_output,
        )


class GlobalCapEnforcementTest(unittest.TestCase):
    def test_submits_past_global_cap_are_skipped(self) -> None:
        sched = IssueScheduler(global_cap=2, per_repo_cap=5)
        self.addCleanup(sched.shutdown)
        starts = [threading.Event() for _ in range(2)]
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(REPO_A, 1, partial(_worker, starts[0], release)),
                "first worker was rejected",
            )
            self.assertTrue(
                sched.submit(REPO_B, 2, partial(_worker, starts[1], release)),
                "second worker was rejected",
            )
            for start in starts:
                self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(), 2)

            # Third submit on a fresh repo still exceeds the global cap.
            self.assertFalse(sched.submit("owner/c", 3, lambda: self.fail(FORBIDDEN_WORKER_MESSAGE)))
            # And on any of the existing repos (with a distinct issue
            # so the duplicate-key rule does not falsely account for
            # the skip).
            self.assertFalse(
                sched.submit(
                    REPO_A,
                    CAP_REJECTION_ISSUE_NUMBER,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                )
            )
            self.assertEqual(sched.active_count(), 2)


class PerRepoCapEnforcementTest(unittest.TestCase):
    def test_submits_past_per_repo_cap_are_skipped(self) -> None:
        sched = IssueScheduler(global_cap=10, per_repo_cap=2)
        self.addCleanup(sched.shutdown)
        starts = [threading.Event() for _ in range(2)]
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(PRIMARY_REPO, 1, partial(_worker, starts[0], release)),
                "first worker was rejected",
            )
            self.assertTrue(
                sched.submit(PRIMARY_REPO, 2, partial(_worker, starts[1], release)),
                "second worker was rejected",
            )
            for start in starts:
                self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(PRIMARY_REPO), 2)

            # A third issue on the same repo is rejected even though
            # the global cap (10) has plenty of room.
            self.assertFalse(sched.submit(PRIMARY_REPO, 3, lambda: self.fail(FORBIDDEN_WORKER_MESSAGE)))
            # A different repo IS still accepted under the global cap.
            start_b = threading.Event()
            self.assertTrue(sched.submit(OTHER_REPO, 4, lambda: _worker(start_b, release)))
            self.assertTrue(start_b.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(
                (
                    sched.active_count(PRIMARY_REPO),
                    sched.active_count(OTHER_REPO),
                ),
                (2, 1),
            )

    def test_per_repo_cap_override_takes_precedence(self) -> None:
        # The per-call override lets a RepoSpec with `parallel_limit=1`
        # cap itself even when the scheduler default is higher.
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
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO,
                    2,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                    per_repo_cap=1,
                )
            )
