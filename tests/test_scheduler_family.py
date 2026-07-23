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
import unittest

from orchestrator.scheduler import IssueScheduler

from tests.scheduler_worker_helpers import (
    _release_on_exit,
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


class FamilyGateTest(unittest.TestCase):
    def test_one_family_worker_per_repo(self) -> None:
        # Per-repo cap is generous so the family slot is the ONLY
        # reason the second submit must be skipped.
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

            # A second family-aware submit on the same repo is rejected
            # even though the per-repo cap (10) has room.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO,
                    SECOND_FAMILY_ISSUE_NUMBER,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                    family=True,
                )
            )
            # A NON-family submit on the same repo IS accepted: the
            # gate is for family workers only.
            start_b = threading.Event()
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    FANOUT_ISSUE_NUMBER,
                    lambda: _worker(start_b, release),
                    family=False,
                )
            )
            self.assertTrue(start_b.wait(timeout=EVENT_TIMEOUT_SECONDS))

            # A family-aware submit on a DIFFERENT repo IS accepted:
            # the gate is per-repo, not global.
            start_c = threading.Event()
            self.assertTrue(
                sched.submit(
                    OTHER_REPO,
                    OTHER_FAMILY_ISSUE_NUMBER,
                    lambda: _worker(start_c, release),
                    family=True,
                )
            )
            self.assertTrue(start_c.wait(timeout=EVENT_TIMEOUT_SECONDS))

    def test_family_slot_clears_on_completion(self) -> None:
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        done = threading.Event()
        self.assertTrue(
            sched.submit(
                PRIMARY_REPO,
                COMPLETED_FAMILY_ISSUE_NUMBER,
                lambda: done.set(),
                family=True,
            )
        )
        self.assertTrue(done.wait(timeout=EVENT_TIMEOUT_SECONDS))

        _wait_until_inactive(
            sched,
            PRIMARY_REPO,
            COMPLETED_FAMILY_ISSUE_NUMBER,
        )

        # Family slot must be released on completion, so a follow-up
        # family-aware submit on the same repo succeeds.
        start = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    FOLLOW_UP_FAMILY_ISSUE_NUMBER,
                    lambda: _worker(start, release),
                    family=True,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
