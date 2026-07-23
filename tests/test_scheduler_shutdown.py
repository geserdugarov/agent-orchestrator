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
from concurrent.futures import Future
from functools import partial
from unittest.mock import patch

from orchestrator.scheduler import IssueScheduler

from tests.scheduler_shutdown_helpers import (
    _CallbackRegistrationRace,
    _ShutdownTrial,
)
from tests.scheduler_coordination_helpers import (
    _release_after,
)
from tests.scheduler_worker_helpers import (
    _finishing_worker,
    _gated_failure,
    _release_on_exit,
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


class ShutdownDrainRaceTest(unittest.TestCase):
    """Regression: ``submit`` used to release the scheduler lock
    between ``executor.submit`` and ``add_done_callback``. A concurrent
    ``shutdown(wait=True)`` could complete its executor drain and its
    single ``reap`` BEFORE the done-callback was registered, so the
    worker's failure was silently dropped (the synchronous-firing
    callback then appended to ``_completed`` AFTER shutdown returned).

    Holding the lock through both steps closes the window. The test
    stresses the race by running submit and shutdown concurrently
    across many short-lived failing workers and asserting that every
    accepted submit's failure ends up in the log.
    """

    def test_blocks_until_callback_is_registered(self) -> None:
        """Deterministic race: gate ``Future.add_done_callback`` on a
        barrier so it cannot finish registering until we release it.
        While submit is blocked inside its lock-held critical section,
        a concurrent ``shutdown(wait=True)`` must NOT make progress;
        if it did, its single ``reap`` would drain an empty list and
        the worker's failure would be silently dropped. The fix holds
        the scheduler lock through both ``executor.submit`` and
        ``add_done_callback`` so the race window is closed."""
        race = _CallbackRegistrationRace()
        with patch.object(Future, "add_done_callback", race):
            with self.assertLogs(SCHEDULER_LOGGER, level=logging.ERROR) as logs:
                race.reach_blocked_state(self)
                race.release_and_assert_finished(self)
                log_output = logs.output

            self.assertTrue(
                any(WORKER_FAILURE in msg for msg in log_output),
                log_output,
            )
            self.assertEqual(race._scheduler.active_count(), 0)

    def test_shutdown_race_logs_each_accepted_failure(self) -> None:
        for trial_number in range(5):
            trial = _ShutdownTrial(trial_number).run(self)
            self.assertGreater(
                trial._accepted,
                0,
                f"trial {trial_number}: no submits were accepted",
            )
            self.assertEqual(
                trial._logged,
                trial._accepted,
                f"trial {trial_number}: shutdown lost a completion",
            )
            self.assertEqual(trial._scheduler.active_count(), 0)


class ShutdownRepeatableWaitTest(unittest.TestCase):
    """Regression: a prior ``shutdown(wait=False)`` used to short-circuit
    a follow-up ``shutdown(wait=True)`` because the handler returned
    early once ``_closed`` was set. The fix drops that early return so
    each call applies its own ``wait`` argument and the trailing reap
    catches any completion that landed between the two shutdowns.
    """

    def test_wait_true_after_false_blocks_until_exit(self) -> None:
        sched = IssueScheduler(global_cap=2, per_repo_cap=2)
        start = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    1,
                    partial(_finishing_worker, start, release, finished),
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            # First call returns immediately, leaving the worker running.
            sched.shutdown(wait=False)
            self.assertFalse(finished.is_set())

            # Second call must actually wait. Release the worker from
            # another thread after a brief delay so the wait is real.
            releaser = threading.Thread(
                target=partial(_release_after, BRIEF_TIMEOUT_SECONDS, release),
            )
            releaser.start()

            sched.shutdown(wait=True)
            # By the time shutdown(wait=True) returns, the worker must
            # have finished -- if the second call had short-circuited,
            # this assertion would fail because the releaser thread is
            # asleep for 50ms.
            self.assertTrue(finished.is_set())
            self.assertEqual(sched.active_count(), 0)
            releaser.join(timeout=EVENT_TIMEOUT_SECONDS)

    def test_wait_true_after_false_drains_completion(self) -> None:
        # A worker that finishes between the two shutdown calls must
        # still have its failure logged by the second call's reap.
        sched = IssueScheduler(global_cap=2, per_repo_cap=2)
        start = threading.Event()
        release = threading.Event()

        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO,
                    7,
                    partial(
                        _gated_failure,
                        start,
                        release,
                        "late worker exploded",
                    ),
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
            sched.shutdown(wait=False)

            with self.assertLogs(SCHEDULER_LOGGER, level=logging.ERROR) as logs:
                release.set()
                # The wait=True call must block until the worker exits
                # and then drain its failure via reap.
                sched.shutdown(wait=True)
                log_output = logs.output
            self.assertTrue(
                any("late worker exploded" in msg for msg in log_output),
                log_output,
            )
