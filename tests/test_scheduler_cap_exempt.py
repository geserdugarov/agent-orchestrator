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


class CapExemptSubmitTest(unittest.TestCase):
    """``submit(cap_exempt=True)`` skips the global and per-repo cap
    counters while still honoring the duplicate-active gate and the
    family mutex. Production uses this for no-agent family buckets so
    blocked / umbrella parent dep-graph walks always get their turn even
    when the parallel caps are saturated by ordinary implementation work.
    """

    def test_cap_exempt_submit_bypasses_global_cap(self) -> None:
        sched = IssueScheduler(global_cap=1, per_repo_cap=10)
        self.addCleanup(sched.shutdown)
        start_a = threading.Event()
        start_b = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    REPO_A,
                    1,
                    lambda: _worker(start_a, release),
                )
            )
            self.assertTrue(start_a.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(), 1)

            # Normal submit on a different repo is rejected: global cap=1
            # and one slot is in use.
            self.assertFalse(
                sched.submit(
                    REPO_B,
                    2,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                )
            )

            # Cap-exempt submit on the same different repo IS accepted
            # even though the global cap is saturated.
            self.assertTrue(
                sched.submit(
                    REPO_B,
                    3,
                    lambda: _worker(start_b, release),
                    cap_exempt=True,
                )
            )
            self.assertTrue(start_b.wait(timeout=EVENT_TIMEOUT_SECONDS))
            # The exempt submit must NOT have inflated the global
            # counter -- still one cap-counted worker in flight.
            self.assertEqual(sched.active_count(), 1)
            # And the exempt submit is visible via `is_active`.
            self.assertTrue(sched.is_active(REPO_B, 3))

    def test_cap_exempt_submit_bypasses_per_repo_cap(self) -> None:
        sched = IssueScheduler(global_cap=10, per_repo_cap=1)
        self.addCleanup(sched.shutdown)
        start_a = threading.Event()
        start_b = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    1,
                    lambda: _worker(start_a, release),
                )
            )
            self.assertTrue(start_a.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(PRIMARY_REPO), 1)

            # Normal submit on the same repo is rejected by per_repo_cap.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO,
                    2,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                )
            )

            # Cap-exempt submit on the same repo IS accepted.
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    3,
                    lambda: _worker(start_b, release),
                    cap_exempt=True,
                )
            )
            self.assertTrue(start_b.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(PRIMARY_REPO), 1)

            # A second cap-exempt submit (different key) on the same
            # repo also bypasses the cap -- exemption is per submit,
            # not a single-slot escape hatch.
            self._assert_second_exempt_submit(sched, release)

    def test_submit_honors_family_mutex(self) -> None:
        # The cap exemption only bypasses the cap counters; family-aware
        # submits still serialize per repo so an exempt no-agent family
        # bucket cannot overlap with a regular (non-exempt) family worker.
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

            # Cap-exempt family submit on the same repo is rejected by
            # the family mutex even though both caps are wide open.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO,
                    SECOND_FAMILY_ISSUE_NUMBER,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                    family=True,
                    cap_exempt=True,
                )
            )

    def test_submit_honors_duplicate_active(self) -> None:
        # A cap-exempt submit for an already-in-flight key is rejected.
        # `is_active` returns True for an exempt-submitted key so a
        # follow-up fanout submit cannot slip past the duplicate gate.
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
                    cap_exempt=True,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertTrue(sched.is_active(PRIMARY_REPO, 7))

            # Duplicate key, cap-exempt: rejected.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO,
                    7,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                    cap_exempt=True,
                )
            )
            # Duplicate key, non-exempt: also rejected.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO,
                    7,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                )
            )

    def test_pool_is_independent_of_global_cap(self) -> None:
        # Regression: a prior implementation sized the cap-exempt
        # executor at ``global_cap``. With ``global_cap=1`` and two
        # no-agent family buckets on different repos, the second
        # exempt submit was accepted past the cap check but then
        # queued at the executor until the first exited -- so
        # exempt-bucket throughput was still transitively capped by
        # ``MAX_PARALLEL_ISSUES_GLOBAL``. The fix sizes the exempt
        # pool independently of ``global_cap`` so multiple exempt
        # buckets can run concurrently regardless of how tight the
        # ordinary cap is.
        sched = IssueScheduler(global_cap=1, per_repo_cap=10)
        self.addCleanup(sched.shutdown)
        start_a = threading.Event()
        start_b = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    REPO_A,
                    0,
                    lambda: _worker(start_a, release),
                    family=True,
                    cap_exempt=True,
                )
            )
            self.assertTrue(start_a.wait(timeout=EVENT_TIMEOUT_SECONDS))

            # A second no-agent family bucket on a DIFFERENT repo must
            # start immediately even though ``global_cap=1`` and the
            # first exempt worker is still in flight. The family
            # mutex is per-repo, so the cross-repo claim is fine; the
            # only thing that could keep the second submit waiting is
            # an executor pool that re-imposes the global cap.
            self.assertTrue(
                sched.submit(
                    REPO_B,
                    0,
                    lambda: _worker(start_b, release),
                    family=True,
                    cap_exempt=True,
                )
            )
            self.assertTrue(
                start_b.wait(timeout=EVENT_TIMEOUT_SECONDS),
                "second exempt family bucket queued behind the first -- exempt executor is still capped by global_cap",
            )

    def test_completion_clears_marker_and_family_slot(self) -> None:
        # Completing an exempt family submit must release BOTH its
        # tracked-set marker (so `is_active` flips back to False) and
        # the family mutex (so the next family submit on this repo is
        # accepted). Without symmetric release, the exempt path would
        # leak markers and starve subsequent family work.
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        done = threading.Event()
        self.assertTrue(
            sched.submit(
                PRIMARY_REPO,
                COMPLETED_FAMILY_ISSUE_NUMBER,
                lambda: done.set(),
                family=True,
                cap_exempt=True,
            )
        )
        self.assertTrue(done.wait(timeout=EVENT_TIMEOUT_SECONDS))

        _wait_until_inactive(
            sched,
            PRIMARY_REPO,
            COMPLETED_FAMILY_ISSUE_NUMBER,
        )
        self.assertFalse(sched.is_active(PRIMARY_REPO, COMPLETED_FAMILY_ISSUE_NUMBER))
        self.assertEqual(sched.active_count(), 0)
        self.assertEqual(sched.active_count(PRIMARY_REPO), 0)

        # A non-exempt family submit on the same repo must now be
        # accepted -- the exempt completion released the family slot.
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

    def _assert_second_exempt_submit(
        self,
        scheduler: IssueScheduler,
        release: threading.Event,
    ) -> None:
        started = threading.Event()
        self.assertTrue(
            scheduler.submit(
                PRIMARY_REPO,
                4,
                lambda: _worker(started, release),
                cap_exempt=True,
            )
        )
        self.assertTrue(started.wait(timeout=EVENT_TIMEOUT_SECONDS))
        self.assertEqual(scheduler.active_count(PRIMARY_REPO), 1)


if __name__ == "__main__":
    unittest.main()
