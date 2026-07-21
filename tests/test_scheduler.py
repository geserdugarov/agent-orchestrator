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
import logging
import threading
import time
import unittest
from concurrent.futures import Future
from functools import partial
from unittest.mock import patch

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


class DuplicateActiveIssueSkipTest(unittest.TestCase):
    def test_same_key_skipped_while_first_in_flight(self) -> None:
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        start = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(PRIMARY_REPO, 1, lambda: _worker(start, release))
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            # Same (repo_slug, issue_number) is rejected even though both
            # global and per-repo caps still have spare slots.
            self.assertFalse(
                sched.submit(PRIMARY_REPO, 1, lambda: self.fail(FORBIDDEN_WORKER_MESSAGE))
            )
            self.assertEqual(sched.active_count(), 1)
            self.assertTrue(sched.is_active(PRIMARY_REPO, 1))

            # Same issue NUMBER on a different repo slug is a different
            # key and IS accepted -- the in-flight set is keyed on the
            # pair, not the number alone.
            start_b = threading.Event()
            self.assertTrue(
                sched.submit(OTHER_REPO, 1, lambda: _worker(start_b, release))
            )
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
            self.assertTrue(
                sched.submit(PRIMARY_REPO, 7, lambda: _worker(start, release))
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

    def test_completion_logs_worker_failure_via_reap(self) -> None:
        sched = IssueScheduler(global_cap=2, per_repo_cap=2)
        self.addCleanup(sched.shutdown)
        done = threading.Event()

        self.assertTrue(sched.submit(
            PRIMARY_REPO,
            9,
            partial(_signaling_failure, done, WORKER_FAILURE),
        ))
        self.assertTrue(done.wait(timeout=EVENT_TIMEOUT_SECONDS))
        # The marker must clear regardless of the exception: a failed
        # worker still hands its slot back.
        _wait_until_inactive(sched, PRIMARY_REPO, 9)
        self.assertFalse(sched.is_active(PRIMARY_REPO, 9))

        with self.assertLogs(SCHEDULER_LOGGER, level=logging.ERROR) as logs:
            count = sched.reap()
        self.assertGreaterEqual(count, 1)
        self.assertTrue(
            any(WORKER_FAILURE in msg for msg in logs.output),
            logs.output,
        )


class GlobalCapEnforcementTest(unittest.TestCase):
    def test_submits_past_global_cap_are_skipped(self) -> None:
        sched = IssueScheduler(global_cap=2, per_repo_cap=5)
        self.addCleanup(sched.shutdown)
        starts = [threading.Event() for _ in range(2)]
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    REPO_A, 1, partial(_worker, starts[0], release)
                )
            )
            self.assertTrue(
                sched.submit(
                    REPO_B, 2, partial(_worker, starts[1], release)
                )
            )
            for start in starts:
                self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(), 2)

            # Third submit on a fresh repo still exceeds the global cap.
            self.assertFalse(
                sched.submit("owner/c", 3, lambda: self.fail(FORBIDDEN_WORKER_MESSAGE))
            )
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
                sched.submit(
                    PRIMARY_REPO, 1, partial(_worker, starts[0], release)
                )
            )
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO, 2, partial(_worker, starts[1], release)
                )
            )
            for start in starts:
                self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(PRIMARY_REPO), 2)

            # A third issue on the same repo is rejected even though
            # the global cap (10) has plenty of room.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO, 3, lambda: self.fail(FORBIDDEN_WORKER_MESSAGE)
                )
            )
            # A different repo IS still accepted under the global cap.
            start_b = threading.Event()
            self.assertTrue(
                sched.submit(
                    OTHER_REPO, 4, lambda: _worker(start_b, release)
                )
            )
            self.assertTrue(start_b.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(PRIMARY_REPO), 2)
            self.assertEqual(sched.active_count(OTHER_REPO), 1)

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
                    PRIMARY_REPO, 1,
                    lambda: _worker(start, release),
                    per_repo_cap=1,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO, 2,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                    per_repo_cap=1,
                )
            )


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
                    PRIMARY_REPO, FIRST_FAMILY_ISSUE_NUMBER,
                    lambda: _worker(start, release),
                    family=True,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            # A second family-aware submit on the same repo is rejected
            # even though the per-repo cap (10) has room.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO, SECOND_FAMILY_ISSUE_NUMBER,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                    family=True,
                )
            )
            # A NON-family submit on the same repo IS accepted: the
            # gate is for family workers only.
            start_b = threading.Event()
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO, FANOUT_ISSUE_NUMBER,
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
                    OTHER_REPO, OTHER_FAMILY_ISSUE_NUMBER,
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
                PRIMARY_REPO, COMPLETED_FAMILY_ISSUE_NUMBER,
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
                    PRIMARY_REPO, FOLLOW_UP_FAMILY_ISSUE_NUMBER,
                    lambda: _worker(start, release),
                    family=True,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))


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
        sched = IssueScheduler(global_cap=2, per_repo_cap=2)
        register_gate = threading.Event()
        real_add = Future.add_done_callback

        def gated_add(self_fut: Future, fn) -> None:
            # Block only on the FIRST registration -- the gated callback
            # might also be registered by `executor.shutdown` internals
            # in some Python versions, so subsequent calls pass straight
            # through to keep shutdown's bookkeeping working.
            if not register_gate.is_set():
                register_gate.wait(timeout=WORKER_TIMEOUT_SECONDS)
            return real_add(self_fut, fn)

        with patch.object(Future, "add_done_callback", gated_add):
            with self.assertLogs(SCHEDULER_LOGGER, level=logging.ERROR) as logs:
                submit_done = threading.Event()

                submitter = threading.Thread(target=partial(
                    _submit_then_signal,
                    sched,
                    _failing_worker,
                    submit_done,
                ))
                submitter.start()
                # Wait until submit is parked inside the gated callback.
                # 0.1s is generous; if the submitter raced past the gate
                # already (impossible with the fix), `submit_done` would
                # also be set.
                time.sleep(0.1)
                self.assertFalse(submit_done.is_set())

                shutdown_done = threading.Event()

                shutter = threading.Thread(target=partial(
                    _shutdown_then_signal,
                    sched,
                    shutdown_done,
                ))
                shutter.start()
                time.sleep(0.1)
                # With the fix, submit holds the scheduler lock through
                # callback registration, so shutdown is blocked on the
                # same lock. Without the fix, shutdown would have run
                # to completion already.
                self.assertFalse(
                    shutdown_done.is_set(),
                    "shutdown must not return while submit is still "
                    "registering its done-callback",
                )

                register_gate.set()
                submitter.join(timeout=WORKER_TIMEOUT_SECONDS)
                shutter.join(timeout=WORKER_TIMEOUT_SECONDS)
                self.assertFalse(submitter.is_alive())
                self.assertFalse(shutter.is_alive())

            self.assertTrue(
                any(WORKER_FAILURE in msg for msg in logs.output),
                logs.output,
            )
            self.assertEqual(sched.active_count(), 0)

    def test_shutdown_race_logs_each_accepted_failure(self) -> None:
        for trial in range(5):
            sched = IssueScheduler(global_cap=8, per_repo_cap=8)
            accepted = 0

            # Submit a head-start batch BEFORE launching the shutdown
            # thread so the race is "shutdown overlaps in-flight
            # submits" rather than "shutdown closes the gate before
            # anyone gets in" -- otherwise an unlucky scheduler that
            # accepts zero submits would render the assertLogs guard
            # vacuously satisfied and hide a real regression.
            head_start = 20
            with self.assertLogs(SCHEDULER_LOGGER, level=logging.ERROR) as logs:
                for issue_number in range(head_start):
                    if sched.submit(
                        f"owner/repo-{trial}",
                        issue_number,
                        _failing_worker,
                    ):
                        accepted += 1
                shutdown_thread = threading.Thread(target=sched.shutdown)
                shutdown_thread.start()
                for issue_number in range(head_start, head_start + 60):
                    if sched.submit(
                        f"owner/repo-{trial}",
                        issue_number,
                        _failing_worker,
                    ):
                        accepted += 1
                shutdown_thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
                self.assertFalse(shutdown_thread.is_alive())

            logged = sum(1 for msg in logs.output if WORKER_FAILURE in msg)
            self.assertGreater(
                accepted, 0,
                f"trial {trial}: no submits were accepted before shutdown ran",
            )
            self.assertEqual(
                logged, accepted,
                f"trial {trial}: accepted={accepted} logged={logged} -- "
                "shutdown drained fewer completions than submits accepted",
            )
            self.assertEqual(sched.active_count(), 0)


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
            self.assertTrue(sched.submit(
                PRIMARY_REPO,
                1,
                partial(_finishing_worker, start, release, finished),
            ))
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
            self.assertTrue(sched.submit(
                PRIMARY_REPO,
                7,
                partial(
                    _gated_failure,
                    start,
                    release,
                    "late worker exploded",
                ),
            ))
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
            sched.shutdown(wait=False)

            with self.assertLogs(SCHEDULER_LOGGER, level=logging.ERROR) as logs:
                release.set()
                # The wait=True call must block until the worker exits
                # and then drain its failure via reap.
                sched.shutdown(wait=True)
            self.assertTrue(
                any("late worker exploded" in msg for msg in logs.output),
                logs.output,
            )


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
                    PRIMARY_REPO, FIRST_FAMILY_ISSUE_NUMBER,
                    lambda: _worker(start, release),
                    family=True,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            with self.assertLogs(
                SCHEDULER_LOGGER, level=logging.INFO,
            ) as logs:
                self.assertFalse(
                    sched.submit(
                        PRIMARY_REPO, SECOND_FAMILY_ISSUE_NUMBER,
                        lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                        family=True,
                    )
                )
            self.assertTrue(
                any(
                    "scheduler skip" in msg and "family_slot_held" in msg
                    and "#101" in msg
                    for msg in logs.output
                ),
                logs.output,
            )

    def test_per_repo_cap_skip_logs_at_info(self) -> None:
        sched = IssueScheduler(global_cap=10, per_repo_cap=5)
        self.addCleanup(sched.shutdown)
        start = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO, 1,
                    lambda: _worker(start, release),
                    per_repo_cap=1,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            with self.assertLogs(
                SCHEDULER_LOGGER, level=logging.INFO,
            ) as logs:
                self.assertFalse(
                    sched.submit(
                        PRIMARY_REPO, 2,
                        lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                        per_repo_cap=1,
                    )
                )
            self.assertTrue(
                any(
                    "scheduler skip" in msg and "per_repo_cap" in msg
                    for msg in logs.output
                ),
                logs.output,
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
                    PRIMARY_REPO, 1, lambda: _worker(start, release),
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            with self.assertLogs(
                SCHEDULER_LOGGER, level=logging.DEBUG,
            ) as logs:
                self.assertFalse(
                    sched.submit(
                        PRIMARY_REPO, 1,
                        lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                    )
                )
            self.assertTrue(
                any(
                    "scheduler skip" in msg and "duplicate_active" in msg
                    for msg in logs.output
                ),
                logs.output,
            )


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
        start_bucket = threading.Event()
        release_bucket = threading.Event()
        bucket_inner_claimed = threading.Event()

        with _release_on_exit(release_bucket):
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
                        (
                            start_bucket,
                            release_bucket,
                            bucket_inner_claimed,
                        ),
                    ),
                    family=True,
                )
            )
            self.assertTrue(start_bucket.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertTrue(bucket_inner_claimed.wait(timeout=EVENT_TIMEOUT_SECONDS))

            # `_active` counts ONLY the executor worker (the bucket
            # submit's sentinel key), not the tracked inner issue.
            self.assertEqual(sched.active_count(), 1)

            # A second fanout submit on a different repo MUST be
            # admitted -- only one executor worker is in flight.
            start_b = threading.Event()
            release_b = threading.Event()
            self.assertTrue(
                sched.submit(
                    OTHER_REPO, 5,
                    lambda: _worker(start_b, release_b),
                )
            )
            self.assertTrue(start_b.wait(timeout=EVENT_TIMEOUT_SECONDS))
            release_b.set()

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
                    PRIMARY_REPO, 7, lambda: _worker(start, release),
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            with sched.track_active(PRIMARY_REPO, 7) as claimed:
                self.assertFalse(
                    claimed,
                    "track_active must report False when the key is "
                    "already in flight elsewhere",
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
                    PRIMARY_REPO, TRACKED_ISSUE_NUMBER,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                )
            )


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
                    REPO_A, 1, lambda: _worker(start_a, release),
                )
            )
            self.assertTrue(start_a.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(), 1)

            # Normal submit on a different repo is rejected: global cap=1
            # and one slot is in use.
            self.assertFalse(
                sched.submit(
                    REPO_B, 2, lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                )
            )

            # Cap-exempt submit on the same different repo IS accepted
            # even though the global cap is saturated.
            self.assertTrue(
                sched.submit(
                    REPO_B, 3,
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
                    PRIMARY_REPO, 1, lambda: _worker(start_a, release),
                )
            )
            self.assertTrue(start_a.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(PRIMARY_REPO), 1)

            # Normal submit on the same repo is rejected by per_repo_cap.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO, 2, lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                )
            )

            # Cap-exempt submit on the same repo IS accepted.
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO, 3,
                    lambda: _worker(start_b, release),
                    cap_exempt=True,
                )
            )
            self.assertTrue(start_b.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(PRIMARY_REPO), 1)

            # A second cap-exempt submit (different key) on the same
            # repo also bypasses the cap -- exemption is per submit,
            # not a single-slot escape hatch.
            start_c = threading.Event()
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO, 4,
                    lambda: _worker(start_c, release),
                    cap_exempt=True,
                )
            )
            self.assertTrue(start_c.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertEqual(sched.active_count(PRIMARY_REPO), 1)

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
                    PRIMARY_REPO, FIRST_FAMILY_ISSUE_NUMBER,
                    lambda: _worker(start, release),
                    family=True,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))

            # Cap-exempt family submit on the same repo is rejected by
            # the family mutex even though both caps are wide open.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO, SECOND_FAMILY_ISSUE_NUMBER,
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
                    PRIMARY_REPO, 7,
                    lambda: _worker(start, release),
                    cap_exempt=True,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))
            self.assertTrue(sched.is_active(PRIMARY_REPO, 7))

            # Duplicate key, cap-exempt: rejected.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO, 7,
                    lambda: self.fail(FORBIDDEN_WORKER_MESSAGE),
                    cap_exempt=True,
                )
            )
            # Duplicate key, non-exempt: also rejected.
            self.assertFalse(
                sched.submit(
                    PRIMARY_REPO, 7,
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
                    REPO_A, 0,
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
                    REPO_B, 0,
                    lambda: _worker(start_b, release),
                    family=True,
                    cap_exempt=True,
                )
            )
            self.assertTrue(
                start_b.wait(timeout=EVENT_TIMEOUT_SECONDS),
                "second exempt family bucket queued behind the first -- "
                "exempt executor is still capped by global_cap",
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
                PRIMARY_REPO, COMPLETED_FAMILY_ISSUE_NUMBER,
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
        self.assertFalse(
            sched.is_active(PRIMARY_REPO, COMPLETED_FAMILY_ISSUE_NUMBER)
        )
        self.assertEqual(sched.active_count(), 0)
        self.assertEqual(sched.active_count(PRIMARY_REPO), 0)

        # A non-exempt family submit on the same repo must now be
        # accepted -- the exempt completion released the family slot.
        start = threading.Event()
        release = threading.Event()
        with _release_on_exit(release):
            self.assertTrue(
                sched.submit(
                    PRIMARY_REPO, FOLLOW_UP_FAMILY_ISSUE_NUMBER,
                    lambda: _worker(start, release),
                    family=True,
                )
            )
            self.assertTrue(start.wait(timeout=EVENT_TIMEOUT_SECONDS))


if __name__ == "__main__":
    unittest.main()
