# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import contextlib
import threading
import time
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import config, workflow

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    KEY_AWAITING_HUMAN,
    KEY_PARENT_NUMBER,
    KEY_PARK_REASON,
    LABEL_BLOCKED,
    LABEL_DECOMPOSING,
    LABEL_IMPLEMENTING,
    LABEL_READY,
    LABEL_UMBRELLA,
    _TEST_SPEC,
)

PROCESS_ISSUE = "_process_issue"
REFRESH_BASE = "_refresh_base_and_worktrees"


_WORKER_ISSUE_NUMBERS = (1, 2, 3)
_ORIGINAL_WORKFLOW_LABEL = FakeGitHubClient.workflow_label
_DEFAULT_PROBE_DELAY_SECONDS = 0.01
_SYNC_TIMEOUT_SECONDS = 5.0
_FAMILY_OVERLAP_DELAY_SECONDS = 0.05
_FAMILY_POLL_DELAY_SECONDS = 0.01
_SERIAL_PROBE_DELAY_SECONDS = 0.02
_FANOUT_ISSUE_NUMBER = 99
_FAMILY_CHILD_ISSUE_NUMBER = 20


def _spec(parallel_limit: int) -> config.RepoSpec:
    return config.RepoSpec(
        slug="acme/widget",
        target_root=Path("/tmp/orchestrator-test-target-root"),
        base_branch="main",
        parallel_limit=parallel_limit,
    )


def _seed_issues(
    client: FakeGitHubClient,
    numbers,
    *,
    label: str = LABEL_IMPLEMENTING,
) -> None:
    for issue_number in numbers:
        client.add_issue(make_issue(issue_number, label=label))


class _ConcurrencyProbe:
    def __init__(
        self,
        *,
        delay: float = _DEFAULT_PROBE_DELAY_SECONDS,
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
        self.release.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        with self._lock:
            self.in_flight -= 1

    def release_after(self, count: int) -> None:
        self.admissions_complete = all(
            self.admitted.acquire(timeout=_SYNC_TIMEOUT_SECONDS)
            for _ in range(count)
        )
        time.sleep(0.1)
        self.release.set()

    def cleanup(self, thread: threading.Thread) -> None:
        self.release.set()
        thread.join(timeout=_SYNC_TIMEOUT_SECONDS)


class _BarrierProcessRecorder:
    def __init__(self, parties: int, *, record_thread: bool = False) -> None:
        self.records: list[int | tuple[int, int]] = []
        self._barrier = threading.Barrier(
            parties,
            timeout=_SYNC_TIMEOUT_SECONDS,
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
        time.sleep(_FAMILY_OVERLAP_DELAY_SECONDS)
        with self._lock:
            self._family_in_flight -= 1

    def _run_fanout(self) -> None:
        with self._lock:
            self._fanout_in_flight += 1
            self.fanout_count += 1
            self.overlap_seen |= self._family_in_flight > 0
        time.sleep(_FAMILY_OVERLAP_DELAY_SECONDS)
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
            self._slow_family_release.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        elif issue.number == _FANOUT_ISSUE_NUMBER:
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
        thread.join(timeout=_SYNC_TIMEOUT_SECONDS)

    def _wait_for_overlap(self) -> None:
        if not self._slow_family_holding.wait(timeout=_SYNC_TIMEOUT_SECONDS):
            raise AssertionError("slow family handler never started")
        if not self._fanout_done.wait(timeout=_SYNC_TIMEOUT_SECONDS):
            raise AssertionError("fanout did not overlap the family handler")


class _SlowFamilyProbe:
    def __init__(self, *, fanout_count: int) -> None:
        self.fanout_done: list[int] = []
        self.released_after_fanout = False
        self._expected_fanout = fanout_count
        self._family_holding = threading.Event()
        self._family_release = threading.Event()
        self._lock = threading.Lock()

    def process(self, _gh, _spec, issue) -> None:
        if issue.number == 1:
            self._family_holding.set()
            self._family_release.wait(timeout=_SYNC_TIMEOUT_SECONDS)
            return
        with self._lock:
            self.fanout_done.append(issue.number)

    def release_after_fanout(self) -> None:
        if not self._family_holding.wait(timeout=_SYNC_TIMEOUT_SECONDS):
            self._family_release.set()
            return
        deadline = time.monotonic() + _SYNC_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.fanout_done) == self._expected_fanout:
                    self.released_after_fanout = True
                    break
            time.sleep(_FAMILY_POLL_DELAY_SECONDS)
        self._family_release.set()

    def cleanup(self, thread: threading.Thread) -> None:
        self._family_release.set()
        thread.join(timeout=_SYNC_TIMEOUT_SECONDS)


def _flaky_workflow_label(_client, issue):
    if getattr(issue, "number", None) == 2:
        raise RuntimeError("simulated label-read failure")
    return _ORIGINAL_WORKFLOW_LABEL(issue)


def _simulate_family_child_state(client, _spec, issue) -> None:
    if issue.number == 10:
        state = client.read_pinned_state(issue)
        for child_number in state.get("children") or []:
            child = client.get_issue(int(child_number))
            child_state = client.read_pinned_state(child)
            if not child_state.get(KEY_PARENT_NUMBER):
                child_state.set(KEY_PARENT_NUMBER, issue.number)
                child_state.set(KEY_AWAITING_HUMAN, False)
                child_state.set(KEY_PARK_REASON, None)
                client.write_pinned_state(child, child_state)
        client.set_workflow_label(issue, LABEL_BLOCKED)
        client.write_pinned_state(issue, state)
        return
    child_state = client.read_pinned_state(issue)
    if child_state.get(KEY_PARENT_NUMBER) or child_state.get(KEY_AWAITING_HUMAN):
        return
    child_state.set(KEY_AWAITING_HUMAN, True)
    child_state.set(KEY_PARK_REASON, "blocked_no_children")
    client.write_pinned_state(issue, child_state)


def _poll_then_raise(gh: FakeGitHubClient):
    yield gh.get_issue(1)
    yield gh.get_issue(2)
    raise RuntimeError("simulated pagination failure")


@contextlib.contextmanager
def _running_thread(target, cleanup):
    thread = threading.Thread(target=target)
    thread.start()
    try:
        yield
    finally:
        cleanup(thread)


class _TrackingGitHubClient(FakeGitHubClient):
    def __init__(
        self,
        get_issue_calls: list[tuple[int, int]],
        calls_lock: threading.Lock,
    ) -> None:
        super().__init__()
        self._get_issue_calls = get_issue_calls
        self._calls_lock = calls_lock

    def get_issue(self, number: int):
        with self._calls_lock:
            self._get_issue_calls.append((number, id(self)))
        return super().get_issue(number)


class _WorkerClientScenario:
    def __init__(self) -> None:
        self.get_issue_calls: list[tuple[int, int]] = []
        self.process_calls: list[tuple[int, int]] = []
        self._calls_lock = threading.Lock()
        self.parent = _TrackingGitHubClient(
            self.get_issue_calls,
            self._calls_lock,
        )
        self.cloned_clients: list[FakeGitHubClient] = []
        _seed_issues(self.parent, _WORKER_ISSUE_NUMBERS)

    def clone_client(self) -> FakeGitHubClient:
        twin = _TrackingGitHubClient(self.get_issue_calls, self._calls_lock)
        _seed_issues(twin, _WORKER_ISSUE_NUMBERS)
        with self._calls_lock:
            self.cloned_clients.append(twin)
        return twin

    def process_issue(self, worker_client, _spec, issue) -> None:
        with self._calls_lock:
            self.process_calls.append((issue.number, id(worker_client)))

    def assert_distinct_worker_clients(self, case: unittest.TestCase) -> None:
        worker_ids = {id(client) for client in self.cloned_clients}
        case.assertEqual(len(self.cloned_clients), len(_WORKER_ISSUE_NUMBERS))
        case.assertEqual(len(worker_ids), len(_WORKER_ISSUE_NUMBERS))
        case.assertNotIn(id(self.parent), worker_ids)

    def assert_worker_refetches(self, case: unittest.TestCase) -> None:
        parent_id = id(self.parent)
        fetched_by_issue = dict(self.get_issue_calls)
        case.assertEqual(len(self.get_issue_calls), len(_WORKER_ISSUE_NUMBERS))
        case.assertNotIn(parent_id, fetched_by_issue.values())
        case.assertEqual(
            dict(self.process_calls),
            fetched_by_issue,
        )
        case.assertEqual(
            sorted(fetched_by_issue),
            list(_WORKER_ISSUE_NUMBERS),
        )


class TickInvokesBaseRefreshTest(unittest.TestCase):
    """`workflow.tick` must drive `_refresh_base_and_worktrees` before any
    issue is processed -- otherwise an in-flight worktree would still be
    anchored at the base SHA from when it was first added.
    """

    def test_refresh_called_once_before_issues(self) -> None:
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))
        refresh = MagicMock()
        process = MagicMock()
        with patch.object(workflow, REFRESH_BASE, refresh), \
             patch.object(workflow, PROCESS_ISSUE, process):
            workflow.tick(gh, _TEST_SPEC)
        refresh.assert_called_once_with(gh, _TEST_SPEC, scheduler=None)
        process.assert_called_once()

    def test_refresh_error_does_not_block_issues(self) -> None:
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))
        refresh = MagicMock(side_effect=RuntimeError("fetch boom"))
        process = MagicMock()
        with patch.object(workflow, REFRESH_BASE, refresh), \
             patch.object(workflow, PROCESS_ISSUE, process):
            workflow.tick(gh, _TEST_SPEC)
        process.assert_called_once()


class TickPerRepoParallelLimitTest(unittest.TestCase):
    """`workflow.tick` must respect `spec.parallel_limit` when fanning per-issue
    work out: a repo configured with `parallel_limit=N` may run up to N
    issues' `_process_issue` calls concurrently, no more, and a single
    failing issue must not stop other eligible issues. The legacy
    `parallel_limit=1` keeps the sequential in-thread behavior so existing
    deployments are unaffected.
    """

    def test_limit_one_processes_sequentially(self) -> None:
        # parallel_limit=1 must keep the legacy in-thread iteration: no
        # overlap, declared issue order preserved, and the call happens on
        # the same thread `tick` was invoked on (no ThreadPoolExecutor).
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2, 3))
        caller_thread = threading.get_ident()
        probe = _ConcurrencyProbe()

        with patch.object(workflow, REFRESH_BASE), \
             patch.object(workflow, PROCESS_ISSUE, side_effect=probe):
            workflow.tick(gh, _spec(parallel_limit=1))

        self.assertEqual(probe.max_in_flight, 1)
        self.assertEqual(probe.order, [1, 2, 3])
        self.assertEqual(probe.thread_ids, {caller_thread})

    def test_limit_caps_concurrent_in_flight(self) -> None:
        # With parallel_limit=2 and 4 eligible issues, the executor must
        # admit at most 2 simultaneously. A blocking fake holds each thread
        # until released so we can observe the steady-state concurrency.
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2, 3, 4))
        probe = _BlockingConcurrencyProbe()
        with _running_thread(
            partial(probe.release_after, 2),
            probe.cleanup,
        ), patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=probe,
        ):
            workflow.tick(gh, _spec(parallel_limit=2))

        self.assertTrue(probe.admissions_complete)
        self.assertEqual(probe.max_in_flight, 2)

    def test_limit_allows_full_concurrency_up_to_cap(self) -> None:
        # With parallel_limit=3 and 3 eligible issues, ALL three must be
        # able to run concurrently. A `threading.Barrier(3)` synchronizes
        # the three workers: if only fewer-than-cap were admitted the
        # barrier would block forever and the test would time out. The
        # bounded `wait` makes that failure mode surface as an assertion.
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2, 3))
        recorder = _BarrierProcessRecorder(3)

        with patch.object(workflow, REFRESH_BASE), \
             patch.object(workflow, PROCESS_ISSUE, side_effect=recorder):
            workflow.tick(gh, _spec(parallel_limit=3))

        self.assertEqual(sorted(recorder.records), [1, 2, 3])

    def test_failing_issue_does_not_stop_other_issues(self) -> None:
        # The exception isolation invariant must hold under the parallel
        # path too: one raising issue must not prevent the other eligible
        # issues from completing.
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2, 3))
        recorder = _IssueProcessRecorder(failing_issue=2)

        with patch.object(workflow, REFRESH_BASE), \
             patch.object(workflow, PROCESS_ISSUE, side_effect=recorder):
            workflow.tick(gh, _spec(parallel_limit=3))

        self.assertEqual(sorted(recorder.processed), [1, 3])

    def test_refresh_runs_once_before_parallel_fanout(self) -> None:
        # The pre-tick base refresh must still happen exactly once per
        # tick, before any issue handler runs, even on the parallel path.
        # Otherwise concurrent worktree fanout could race the still-stale
        # base SHA into the per-issue merges.
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2))
        refresh = MagicMock()
        recorder = _RefreshOrderRecorder(refresh)

        with patch.object(workflow, REFRESH_BASE, refresh), \
             patch.object(workflow, PROCESS_ISSUE, side_effect=recorder):
            workflow.tick(gh, _spec(parallel_limit=2))

        refresh.assert_called_once_with(
            gh, _spec(parallel_limit=2), scheduler=None,
        )
        # Every worker observed refresh.call_count == 1 -- i.e. the refresh
        # completed BEFORE any `_process_issue` started.
        self.assertEqual(recorder.calls, [1, 1])


class TickFamilySchedulingTest(unittest.TestCase):
    """Family-aware work serializes internally while fanout stays parallel."""

    def test_family_aware_stages_never_overlap(self) -> None:
        # Family-aware labels (decomposing, blocked, umbrella, and unlabeled
        # pickup) write across parent/child boundaries -- the parent's
        # `_handle_decomposing` recovery seeds `parent_number` on each
        # recorded child, while `_handle_blocked` would otherwise park the
        # same child as `blocked_no_children`. Running two of these
        # concurrently raced the writes (the child's late
        # `awaiting_human=True` write clobbered the parent's just-seeded
        # `parent_number`). `tick()` must therefore hold a tick-local
        # lock around the family-aware handlers so no two run at the same
        # time -- AND must let non-family-aware workers run alongside,
        # so a slow decomposing handler does not block unrelated
        # implementing / validating work in the same tick.
        #
        # `ready` is deliberately NOT family-aware (it only writes its own
        # state and recurses into `_handle_implementing`) -- the separate
        # `test_ready_issues_fan_out_concurrently` test pins that
        # contract down.
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_DECOMPOSING))
        gh.add_issue(make_issue(2, label=LABEL_BLOCKED))
        gh.add_issue(make_issue(4, label=LABEL_UMBRELLA))
        # An unlabeled issue routes through `_handle_pickup` -> decomposer
        # and is therefore family-aware too.
        gh.add_issue(make_issue(5, label=None))
        # A non-family-aware label that MUST fan out to a worker thread
        # AND must be allowed to overlap with the family-aware bucket.
        gh.add_issue(
            make_issue(_FANOUT_ISSUE_NUMBER, label=LABEL_IMPLEMENTING),
        )

        probe = _FamilyOverlapProbe(fanout_issue=_FANOUT_ISSUE_NUMBER)

        # parallel_limit=5 and no `global_semaphore` means every submission
        # gets its own worker thread; the family lock is the ONLY thing
        # preventing family-aware handlers from overlapping with each
        # other, and the fanout worker is free to run alongside whichever
        # family handler currently holds the lock.
        with patch.object(workflow, REFRESH_BASE), \
             patch.object(workflow, PROCESS_ISSUE, side_effect=probe):
            workflow.tick(gh, _spec(parallel_limit=5))

        # Four family-aware issues observed; the family lock kept them
        # from overlapping with each other.
        self.assertEqual(probe.family_count, 4)
        self.assertEqual(probe.family_max_in_flight, 1)
        self.assertEqual(probe.fanout_count, 1)
        # Fanout handler ran concurrently with at least one family
        # handler. Without the overlap fix (family draining before
        # fanout starts), `overlap_seen` would stay False.
        self.assertTrue(
            probe.overlap_seen,
            "family bucket and fanout bucket did not overlap -- regression "
            "to draining family synchronously before the executor starts?",
        )

    def test_ready_issues_fan_out_concurrently(self) -> None:
        # `ready` is NOT family-aware -- `_handle_ready` only writes its
        # own pinned state, then recurses into `_handle_implementing`
        # which runs the long-running dev-agent work. Putting `ready` in
        # the family bucket would force every ready->implementing job to
        # run sequentially on the caller thread, defeating the
        # `parallel_limit > 1` concurrency goal of issue #115. This test
        # pins that contract: with three `ready` issues and
        # `parallel_limit=3`, all three must be able to enter
        # `_process_issue` concurrently.
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2, 3), label=LABEL_READY)

        caller_thread = threading.get_ident()
        recorder = _BarrierProcessRecorder(3, record_thread=True)

        with patch.object(workflow, REFRESH_BASE), \
             patch.object(workflow, PROCESS_ISSUE, side_effect=recorder):
            workflow.tick(gh, _spec(parallel_limit=3))

        passed = recorder.records
        passed_numbers = sorted(record[0] for record in passed)
        self.assertEqual(passed_numbers, [1, 2, 3])
        # All three ran on worker threads, not the caller thread.
        for _issue_number, thread_id in passed:
            self.assertNotEqual(
                thread_id, caller_thread,
                "ready issues must fan out to worker threads, not the caller",
            )

    def test_label_error_does_not_abort_others(self) -> None:
        # Per-issue exception isolation must extend to the partition's
        # label read. The reviewer's reproducer: if `gh.workflow_label`
        # raises on one issue while classifying for parallel fanout, the
        # partition loop aborts and EVERY other eligible issue this tick
        # goes unprocessed -- a regression of the existing per-issue
        # isolation invariant. The fix catches the read, logs it, and
        # routes the offending issue into the family bucket where the
        # per-issue try/except picks up any sustained failure.
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2, 3))
        recorder = _IssueProcessRecorder()
        # Issue #2 still ends up in `_process_issue` via the family
        # bucket (the partition routes label-read failures there) so the
        # fake_process gets called for it too -- but ALSO for #1 and #3,
        # proving the other issues weren't aborted.

        with patch.object(
            FakeGitHubClient,
            "workflow_label",
            _flaky_workflow_label,
        ), \
             patch.object(workflow, REFRESH_BASE), \
             patch.object(workflow, PROCESS_ISSUE, side_effect=recorder):
            workflow.tick(gh, _spec(parallel_limit=3))

        # All three issues were attempted -- the partition did not abort
        # after the bad label read on #2.
        self.assertEqual(sorted(recorder.processed), [1, 2, 3])

    def test_family_bucket_uses_one_slot(self) -> None:
        # Reviewer's exact reproducer: with `parallel_limit=2`, two
        # family-aware issues, and one fanout issue, an earlier draft
        # that submitted per-family-issue futures plus a shared lock
        # let the slow family handler hold one worker slot while the
        # second family future occupied the OTHER worker slot blocking
        # on the lock -- the fanout issue stayed queued until the slow
        # family handler exited. The drain-task design folds the whole
        # family bucket into one future so it consumes exactly one
        # executor slot regardless of how many family-aware issues are
        # pending, leaving the other limit-1 slots free for fanout.
        #
        # The test holds the FIRST family handler inside `_process_issue`
        # until the fanout handler completes; without the drain-task fix
        # the fanout handler would be queued and never run, the wait
        # below would time out, and the assertion would fire.
        gh = FakeGitHubClient()
        # Two family-aware issues. The first is slow; the second
        # must wait for the first because the family bucket runs them
        # sequentially in one drain task.
        gh.add_issue(make_issue(1, label=LABEL_DECOMPOSING))
        gh.add_issue(make_issue(2, label=LABEL_BLOCKED))
        # One fanout issue that MUST advance while the slow family
        # handler is still inside `_process_issue`.
        gh.add_issue(
            make_issue(_FANOUT_ISSUE_NUMBER, label=LABEL_IMPLEMENTING),
        )
        probe = _FamilySlotProbe()
        with _running_thread(
            probe.release_after_fanout,
            probe.cleanup,
        ):
            # parallel_limit=2 + 3 submissions total. Family bucket =
            # one drain task = one slot. Fanout = one task = one slot.
            # The second family issue stays inside the drain task (not
            # a separate executor slot), so the fanout's slot is free
            # while issue #1 is held.
            with patch.object(workflow, REFRESH_BASE), patch.object(
                workflow,
                PROCESS_ISSUE,
                side_effect=probe.process,
            ):
                workflow.tick(gh, _spec(parallel_limit=2))

        if probe.releaser_errors:
            raise probe.releaser_errors[0]

        # All three issues handled.
        self.assertEqual(
            sorted(probe.observed_order),
            [1, 2, _FANOUT_ISSUE_NUMBER],
        )
        # Family #2 ran AFTER family #1 (drain task is sequential).
        first_family_index = probe.observed_order.index(1)
        second_family_index = probe.observed_order.index(2)
        self.assertLess(
            first_family_index,
            second_family_index,
            probe.observed_order,
        )
        # And the fanout entered `_process_issue` BEFORE family #1
        # exited (the releaser only released after `fanout_done` was
        # set, which the fanout handler sets on entry).
        fanout_index = probe.observed_order.index(_FANOUT_ISSUE_NUMBER)
        self.assertLess(
            fanout_index,
            second_family_index,
            probe.observed_order,
        )

    def test_slow_family_does_not_block_fanout(self) -> None:
        # Reviewer's reproducer: a single long decomposing / unlabeled-
        # pickup agent run must NOT block the other workers in the same
        # tick. With the family lock holding the family bucket on one
        # worker, the other (limit-1) workers must still be able to
        # advance unrelated implementing / validating issues -- otherwise
        # a mixed-stage tick collapses back to serial in practice.
        gh = FakeGitHubClient()
        # One slow family-aware issue. The handler holds inside
        # `_process_issue` until released by the test; without the
        # overlap fix this would freeze every other worker.
        gh.add_issue(make_issue(1, label=LABEL_DECOMPOSING))
        # Several fanout issues that MUST advance while the family
        # handler is still running.
        _seed_issues(gh, (10, 11, 12))
        probe = _SlowFamilyProbe(fanout_count=3)
        with _running_thread(
            probe.release_after_fanout,
            probe.cleanup,
        ), patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=probe.process,
        ):
            workflow.tick(gh, _spec(parallel_limit=4))

        # All three fanout issues completed while the family handler
        # was still inside `_process_issue` -- exactly the property the
        # reviewer asked for. Without the overlap fix, this list would
        # be empty (or only one entry, the lucky fanout that grabbed
        # the caller thread).
        self.assertTrue(probe.released_after_fanout)
        self.assertEqual(sorted(probe.fanout_done), [10, 11, 12])

    def test_family_stages_do_not_race_child_state(
        self,
    ) -> None:
        # Regression for the reproducer the reviewer flagged: a parent
        # `decomposing` recovery seeded `parent_number` on a child while a
        # concurrent `blocked` tick on the same child cleared it and
        # wrote `awaiting_human=True` + `park_reason=blocked_no_children`.
        # With the tick-local family lock in place, the two family-aware
        # handlers cannot overlap regardless of which worker picks each
        # one up -- whichever runs first, the parent's repair is the
        # final word and the child's pinned state retains `parent_number`
        # without the stale park flags.
        gh = FakeGitHubClient()
        # Parent #10 carries the half-finished-decomposition recovery
        # markers (`expected_children_count=1`, `children=[20]`) so its
        # `_handle_decomposing` enters the repair branch and seeds the
        # child's state. Child #20 is labeled `blocked` with empty pinned
        # state, so its `_handle_blocked` would normally park
        # `blocked_no_children` and clobber the parent's seed.
        gh.add_issue(make_issue(10, label=LABEL_DECOMPOSING))
        gh.add_issue(make_issue(_FAMILY_CHILD_ISSUE_NUMBER, label=LABEL_BLOCKED))
        gh.seed_state(
            10,
            expected_children_count=1,
            children=[_FAMILY_CHILD_ISSUE_NUMBER],
            umbrella=None,
        )

        with patch.object(workflow, REFRESH_BASE), \
             patch.object(
                 workflow,
                 PROCESS_ISSUE,
                 side_effect=_simulate_family_child_state,
             ):
            workflow.tick(gh, _spec(parallel_limit=4))

        # Child's final state retains the parent's seed and is not parked.
        # The family lock guarantees the two handlers ran sequentially
        # in some order; either order produces this final state because
        # the parent's repair either runs first (child sees parent_number
        # set and returns early) or last (parent's write is final).
        child_state = gh.pinned_data(_FAMILY_CHILD_ISSUE_NUMBER)
        self.assertEqual(child_state.get(KEY_PARENT_NUMBER), 10)
        self.assertFalse(child_state.get(KEY_AWAITING_HUMAN))
        self.assertIsNone(child_state.get(KEY_PARK_REASON))


class TickGlobalSchedulingTest(unittest.TestCase):
    """Host limits and worker-client isolation apply across repo fanout."""

    def test_no_eligible_issues_is_a_noop(self) -> None:
        # An empty pollable list must not spin up worker threads or raise.
        gh = FakeGitHubClient()
        process = MagicMock()
        with patch.object(workflow, REFRESH_BASE), \
             patch.object(workflow, PROCESS_ISSUE, process):
            workflow.tick(gh, _spec(parallel_limit=4))
        process.assert_not_called()

    def test_global_semaphore_clamps_concurrency(self) -> None:
        # The `global_semaphore` parameter is the host-wide ceiling threaded
        # in by `main._run_tick`. It must clamp concurrent `_process_issue`
        # calls regardless of how high `spec.parallel_limit` was
        # configured: a spec with parallel_limit=4 plus a semaphore sized
        # 2 must never have more than 2 issues in flight at once, even
        # though the per-repo executor admits 4 worker threads.
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2, 3, 4))
        probe = _BlockingConcurrencyProbe()
        with _running_thread(
            partial(probe.release_after, 2),
            probe.cleanup,
        ), patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=probe,
        ):
            workflow.tick(
                gh,
                _spec(parallel_limit=4),
                global_semaphore=threading.BoundedSemaphore(2),
            )

        # Even though parallel_limit=4 would otherwise let 4 issues run in
        # parallel, the semaphore cap of 2 must hold.
        self.assertTrue(probe.admissions_complete)
        self.assertEqual(probe.max_in_flight, 2)

    def test_global_limit_one_serializes_processing(self) -> None:
        # With a size-1 semaphore the `_process_issue` calls must run one
        # at a time regardless of `parallel_limit`. This is the workflow-
        # level guarantee that backs `MAX_PARALLEL_ISSUES_GLOBAL=1`: even
        # with multiple worker threads spun up, only one is ever inside
        # `_process_issue`.
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2, 3))
        probe = _ConcurrencyProbe(delay=_SERIAL_PROBE_DELAY_SECONDS)

        with patch.object(workflow, REFRESH_BASE), \
             patch.object(workflow, PROCESS_ISSUE, side_effect=probe):
            workflow.tick(
                gh,
                _spec(parallel_limit=5),
                global_semaphore=threading.BoundedSemaphore(1),
            )

        self.assertEqual(probe.max_in_flight, 1)

    def test_workers_use_own_clients_and_refetch(
        self,
    ) -> None:
        # PyGithub's `Requester` is not documented thread-safe; sharing a
        # single client across worker threads can interleave concurrent
        # request setup. The parallel path must therefore (a) call
        # `gh._for_worker_thread()` once per submitted issue so each
        # worker gets its own client, and (b) refetch the Issue via the
        # WORKER'S client so the Issue's parent requester chain matches
        # the thread that actually drives it.
        scenario = _WorkerClientScenario()

        with (
            patch.object(
                scenario.parent,
                "_for_worker_thread",
                side_effect=scenario.clone_client,
            ),
            patch.object(workflow, REFRESH_BASE),
            patch.object(
                workflow,
                PROCESS_ISSUE,
                side_effect=scenario.process_issue,
            ),
        ):
            workflow.tick(scenario.parent, _spec(parallel_limit=3))

        scenario.assert_distinct_worker_clients(self)
        scenario.assert_worker_refetches(self)

    def test_limit_one_does_not_clone_per_issue(self) -> None:
        # Sequential mode runs on the caller thread; the PyGithub thread
        # safety rationale does not apply, so the legacy path must not
        # call `_for_worker_thread()` (avoids an unnecessary token + repo
        # round-trip for every issue on every tick).
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2, 3))
        clone = MagicMock(side_effect=AssertionError(
            "_for_worker_thread must not be called on the sequential path",
        ))
        with patch.object(gh, "_for_worker_thread", clone), \
             patch.object(workflow, REFRESH_BASE), \
             patch.object(workflow, PROCESS_ISSUE):
            workflow.tick(gh, _spec(parallel_limit=1))
        clone.assert_not_called()

    def test_limit_one_processes_issues_before_error(self) -> None:
        # Legacy invariant: with parallel_limit=1, the loop iterates the
        # generator directly so any issue yielded BEFORE an enumeration
        # failure (PyGithub pagination error, closed-issue sweep raise) is
        # still processed. Materializing the iterator upfront would lose
        # those already-yielded issues. Generator-style fake raises
        # mid-iteration to pin the streaming contract down.
        gh = FakeGitHubClient()
        _seed_issues(gh, (1, 2, 3))
        recorder = _IssueProcessRecorder()

        with patch.object(
            gh,
            "list_pollable_issues",
            partial(_poll_then_raise, gh),
        ), \
             patch.object(workflow, REFRESH_BASE), \
             patch.object(workflow, PROCESS_ISSUE, side_effect=recorder):
            # The enumeration failure is not caught inside `tick` (it lives
            # at the per-repo boundary in `main._run_tick`), but the issues
            # yielded BEFORE the raise must still have been processed.
            with self.assertRaises(RuntimeError):
                workflow.tick(gh, _spec(parallel_limit=1))

        self.assertEqual(recorder.processed, [1, 2])


if __name__ == "__main__":
    unittest.main()
