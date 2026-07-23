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
from functools import partial

from orchestrator.scheduler import IssueScheduler

from tests.scheduler_coordination_helpers import (
    _tracked_worker,
)
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


class TrackActiveContextManagerTest(unittest.TestCase):
    """`track_active` is what the workflow's family-bucket drain uses to
    keep `is_active(repo, n)` reporting True for the issue currently
    being processed inside the bucket. The claim lives in a dedicated
    set (``_tracked``) so it does NOT inflate the global-cap counter
    or the per-repo counter -- the bucket's parent submit already
    accounts for the one executor worker; double-counting would let a
    single bucket starve unrelated fanout submits.
    """

    def test_marks_key_active_for_the_duration(self) -> None:
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        self.assertFalse(sched.is_active(PRIMARY_REPO, 7))
        with sched.track_active(PRIMARY_REPO, 7) as claimed:
            self.assertTrue(claimed)
            self.assertTrue(sched.is_active(PRIMARY_REPO, 7))
        self.assertFalse(sched.is_active(PRIMARY_REPO, 7))

    def test_does_not_bump_per_repo_counter(self) -> None:
        # The bucket's parent submit is what counts toward per_repo
        # budget; track_active's inner claim is purely for is_active
        # reporting (refresh-skip).
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        self.assertEqual(sched.active_count(PRIMARY_REPO), 0)
        with sched.track_active(PRIMARY_REPO, 7) as claimed:
            self.assertTrue(claimed)
            self.assertEqual(sched.active_count(PRIMARY_REPO), 0)

    def test_does_not_count_toward_global_cap(self) -> None:
        # With `global_cap=2`, a single family bucket worker running and
        # tracking one inner issue must NOT exhaust the global cap: only
        # one executor worker is actually running, so a second fanout
        # submit on a different repo / issue must still be admitted.
        # Regression: previously `track_active` added to `_active` and
        # `len(self._active)` inflated past the cap.
        sched = IssueScheduler(global_cap=2, per_repo_cap=2)
        self.addCleanup(sched.shutdown)
        bucket_gates = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        with _release_on_exit(bucket_gates[1]):
            # Bucket submit (family-aware) takes one executor slot.
            self.assertTrue(
                sched.submit(
                    "owner/family",
                    0,
                    partial(
                        _tracked_worker,
                        sched,
                        "owner/family",
                        100,
                        bucket_gates,
                    ),
                    family=True,
                )
            )
            self.assertTrue(bucket_gates[0].wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertTrue(bucket_gates[2].wait(timeout=EVENT_TIMEOUT_SECONDS))

            # `_active` counts ONLY the executor worker (the bucket
            # submit's sentinel key), not the tracked inner issue.
            self.assertEqual(sched.active_count(), 1)

            # A second fanout submit on a different repo MUST be
            # admitted -- only one executor worker is in flight.
            fanout_gates = (threading.Event(), threading.Event())
            self.assertTrue(
                sched.submit(
                    OTHER_REPO,
                    5,
                    lambda: _worker(*fanout_gates),
                )
            )
            self.assertTrue(fanout_gates[0].wait(timeout=EVENT_TIMEOUT_SECONDS))
            fanout_gates[1].set()

    def test_duplicate_claim_keeps_existing_marker(
        self,
    ) -> None:
        # The drain must skip `_process_issue` when `claimed` is False;
        # otherwise two workers could run the same handler concurrently.
        # The cleanup hook must leave the original owner's marker alone.
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        start = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    7,
                    lambda: _worker(start, release),
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            with sched.track_active(PRIMARY_REPO, 7) as claimed:
                self.assertFalse(
                    claimed,
                    "track_active must report False when the key is already in flight elsewhere",
                )
                self.assertTrue(sched.is_active(PRIMARY_REPO, 7))
            # The original owner's marker survives the inner exit.
            self.assertTrue(sched.is_active(PRIMARY_REPO, 7))

    def test_submit_rejects_fanout_for_tracked_issue(self) -> None:
        # A fanout submit for an issue currently held by track_active
        # must be skipped via the duplicate-active gate -- otherwise a
        # cross-tick relabel could let two workers run the same handler
        # concurrently (one inside the family bucket, one as fanout).
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        start = threading.Event()
        release = threading.Event()
        inner_claimed = threading.Event()

        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    0,
                    partial(
                        _tracked_worker,
                        sched,
                        PRIMARY_REPO,
                        TRACKED_ISSUE_NUMBER,
                        (start, release, inner_claimed),
                    ),
                    family=True,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertTrue(inner_claimed.wait(timeout=EVENT_TIMEOUT_SECONDS))

            # A fanout submit for the tracked issue must be rejected.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO,
                    TRACKED_ISSUE_NUMBER,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                )
            )
