# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Concurrency probes for workflow tick tests."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from tests import workflow_tick_parallel_test_support as support


class _ConcurrencyProbe:
    def __init__(
        self,
        *,
        delay: float = support._DEFAULT_PROBE_DELAY_SECONDS,
    ) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.order: list[int] = []
        self.thread_ids: set[int] = set()
        self._delay = delay
        self._lock = threading.Lock()

    def __call__(self, _gh, _spec, issue) -> None:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.order.append(issue.number)
            self.thread_ids.add(threading.get_ident())
        time.sleep(self._delay)
        with self._lock:
            self.in_flight -= 1


class _BlockingConcurrencyProbe:
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.admissions_complete = False
        self.admitted = threading.Semaphore(0)
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, _gh, _spec, _issue) -> None:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.admitted.release()
        self.release.wait(timeout=support._SYNC_TIMEOUT_SECONDS)
        with self._lock:
            self.in_flight -= 1

    def release_after(self, count: int) -> None:
        self.admissions_complete = all(
            self.admitted.acquire(timeout=support._SYNC_TIMEOUT_SECONDS)
            for _ in range(count)
        )
        time.sleep(0.1)
        self.release.set()

    def cleanup(self, thread: threading.Thread) -> None:
        self.release.set()
        thread.join(timeout=support._SYNC_TIMEOUT_SECONDS)


class _BarrierProcessRecorder:
    def __init__(self, parties: int, *, record_thread: bool = False) -> None:
        self.records: list[int | tuple[int, int]] = []
        self._barrier = threading.Barrier(
            parties,
            timeout=support._SYNC_TIMEOUT_SECONDS,
        )
        self._record_thread = record_thread
        self._lock = threading.Lock()

    def __call__(self, _gh, _spec, issue) -> None:
        self._barrier.wait()
        record = (
            (issue.number, threading.get_ident())
            if self._record_thread
            else issue.number
        )
        with self._lock:
            self.records.append(record)

    def assert_worker_records(
        self,
        case,
        caller_thread: int,
    ) -> None:
        case.assertEqual(
            sorted(record[0] for record in self.records),
            list(support._WORKER_ISSUE_NUMBERS),
        )
        case.assertTrue(
            all(
                thread_id != caller_thread
                for _issue_number, thread_id in self.records
            ),
            "ready issues must fan out to worker threads, not the caller",
        )


class _IssueProcessRecorder:
    def __init__(self, *, failing_issue: int | None = None) -> None:
        self.processed: list[int] = []
        self._failing_issue = failing_issue
        self._lock = threading.Lock()

    def __call__(self, _gh, _spec, issue) -> None:
        if issue.number == self._failing_issue:
            raise RuntimeError(f"simulated issue #{issue.number} failure")
        with self._lock:
            self.processed.append(issue.number)


class _RefreshOrderRecorder:
    def __init__(self, refresh_mock: MagicMock) -> None:
        self.calls: list[int] = []
        self._refresh_mock = refresh_mock
        self._lock = threading.Lock()

    def __call__(self, _gh, _spec, _issue) -> None:
        with self._lock:
            self.calls.append(self._refresh_mock.call_count)


class _FamilyOverlapProbe:
    def __init__(self, *, fanout_issue: int) -> None:
        self.family_count = 0
        self.family_max_in_flight = 0
        self.fanout_count = 0
        self.overlap_seen = False
        self._family_in_flight = 0
        self._fanout_in_flight = 0
        self._fanout_issue = fanout_issue
        self._lock = threading.Lock()

    def __call__(self, _gh, _spec, issue) -> None:
        if issue.number == self._fanout_issue:
            self._run_fanout()
        else:
            self._run_family()

    def _run_family(self) -> None:
        with self._lock:
            self._family_in_flight += 1
            self.family_max_in_flight = max(
                self.family_max_in_flight,
                self._family_in_flight,
            )
            self.family_count += 1
            self.overlap_seen |= self._fanout_in_flight > 0
        time.sleep(support._FAMILY_OVERLAP_DELAY_SECONDS)
        with self._lock:
            self._family_in_flight -= 1

    def _run_fanout(self) -> None:
        with self._lock:
            self._fanout_in_flight += 1
            self.fanout_count += 1
            self.overlap_seen |= self._family_in_flight > 0
        time.sleep(support._FAMILY_OVERLAP_DELAY_SECONDS)
        with self._lock:
            self._fanout_in_flight -= 1


class _FamilySlotProbe:
    def __init__(self) -> None:
        self.observed_order: list[int] = []
        self.releaser_errors: list[BaseException] = []
        self._slow_family_holding = threading.Event()
        self._slow_family_release = threading.Event()
        self._fanout_done = threading.Event()
        self._lock = threading.Lock()

    def process(self, _gh, _spec, issue) -> None:
        with self._lock:
            self.observed_order.append(issue.number)
        if issue.number == 1:
            self._slow_family_holding.set()
            self._slow_family_release.wait(timeout=support._SYNC_TIMEOUT_SECONDS)
        elif issue.number == support._FANOUT_ISSUE_NUMBER:
            self._fanout_done.set()

    def release_after_fanout(self) -> None:
        try:
            self._wait_for_overlap()
        except BaseException as error:
            self.releaser_errors.append(error)
        finally:
            self._slow_family_release.set()

    def cleanup(self, thread: threading.Thread) -> None:
        self._slow_family_release.set()
        thread.join(timeout=support._SYNC_TIMEOUT_SECONDS)

    def _wait_for_overlap(self) -> None:
        if not self._slow_family_holding.wait(timeout=support._SYNC_TIMEOUT_SECONDS):
            raise AssertionError("slow family handler never started")
        if not self._fanout_done.wait(timeout=support._SYNC_TIMEOUT_SECONDS):
            raise AssertionError("fanout did not overlap the family handler")
