# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import _TEST_SPEC


class TickInvokesBaseRefreshTest(unittest.TestCase):
    """`workflow.tick` must drive `_refresh_base_and_worktrees` before any
    issue is processed -- otherwise an in-flight worktree would still be
    anchored at the base SHA from when it was first added.
    """

    def test_refresh_called_once_before_issues(self) -> None:
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))
        refresh = MagicMock()
        process = MagicMock()
        with patch.object(workflow, "_refresh_base_and_worktrees", refresh), \
             patch.object(workflow, "_process_issue", process):
            workflow.tick(gh, _TEST_SPEC)
        refresh.assert_called_once_with(gh, _TEST_SPEC, scheduler=None)
        process.assert_called_once()

    def test_refresh_exception_does_not_block_issue_processing(self) -> None:
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))
        refresh = MagicMock(side_effect=RuntimeError("fetch boom"))
        process = MagicMock()
        with patch.object(workflow, "_refresh_base_and_worktrees", refresh), \
             patch.object(workflow, "_process_issue", process):
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

    def _spec(self, parallel_limit: int) -> config.RepoSpec:
        return config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
            parallel_limit=parallel_limit,
        )

    def test_limit_one_processes_sequentially_in_caller_thread(self) -> None:
        # parallel_limit=1 must keep the legacy in-thread iteration: no
        # overlap, declared issue order preserved, and the call happens on
        # the same thread `tick` was invoked on (no ThreadPoolExecutor).
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))
        caller_thread = threading.get_ident()
        in_flight = 0
        max_in_flight = 0
        order: list[int] = []
        worker_threads: set[int] = set()
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                order.append(issue.number)
                worker_threads.add(threading.get_ident())
            time.sleep(0.01)
            with lock:
                in_flight -= 1

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=1))

        self.assertEqual(max_in_flight, 1)
        self.assertEqual(order, [1, 2, 3])
        self.assertEqual(worker_threads, {caller_thread})

    def test_limit_caps_concurrent_in_flight(self) -> None:
        # With parallel_limit=2 and 4 eligible issues, the executor must
        # admit at most 2 simultaneously. A blocking fake holds each thread
        # until released so we can observe the steady-state concurrency.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3, 4):
            gh.add_issue(make_issue(n, label="implementing"))
        in_flight = 0
        max_in_flight = 0
        # Each enter() ticks the counter and waits up to a bounded timeout
        # so a regression that admitted more than the cap surfaces here
        # rather than deadlocking the suite.
        lock = threading.Lock()
        admitted = threading.Semaphore(0)
        release = threading.Event()

        def fake_process(_gh, _spec, _issue) -> None:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            admitted.release()
            # Hold the thread so up to `limit` workers pile up before any
            # of them exits and frees a slot.
            release.wait(timeout=5.0)
            with lock:
                in_flight -= 1

        def release_when_two_admitted() -> None:
            # Wait until exactly 2 workers are in-flight, hold briefly to
            # let the executor try (and fail) to admit a third, then let
            # all workers drain.
            for _ in range(2):
                self.assertTrue(
                    admitted.acquire(timeout=5.0),
                    "fake_process never admitted 2 workers within timeout",
                )
            time.sleep(0.1)
            release.set()

        releaser = threading.Thread(target=release_when_two_admitted)
        releaser.start()
        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(parallel_limit=2))
        finally:
            release.set()
            releaser.join(timeout=5.0)

        self.assertEqual(max_in_flight, 2)

    def test_limit_allows_full_concurrency_up_to_cap(self) -> None:
        # With parallel_limit=3 and 3 eligible issues, ALL three must be
        # able to run concurrently. A `threading.Barrier(3)` synchronizes
        # the three workers: if only fewer-than-cap were admitted the
        # barrier would block forever and the test would time out. The
        # bounded `wait` makes that failure mode surface as an assertion.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))
        barrier = threading.Barrier(3)
        passed: list[int] = []
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            barrier.wait(timeout=5.0)
            with lock:
                passed.append(issue.number)

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=3))

        self.assertEqual(sorted(passed), [1, 2, 3])

    def test_failing_issue_does_not_stop_other_issues(self) -> None:
        # The exception isolation invariant must hold under the parallel
        # path too: one raising issue must not prevent the other eligible
        # issues from completing.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))
        processed: list[int] = []
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            if issue.number == 2:
                raise RuntimeError("simulated issue #2 failure")
            with lock:
                processed.append(issue.number)

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=3))

        self.assertEqual(sorted(processed), [1, 3])

    def test_refresh_runs_once_before_parallel_fanout(self) -> None:
        # The pre-tick base refresh must still happen exactly once per
        # tick, before any issue handler runs, even on the parallel path.
        # Otherwise concurrent worktree fanout could race the still-stale
        # base SHA into the per-issue merges.
        import threading
        from unittest.mock import MagicMock

        gh = FakeGitHubClient()
        for n in (1, 2):
            gh.add_issue(make_issue(n, label="implementing"))
        refresh_seen_by_worker: list[int] = []
        refresh = MagicMock()
        lock = threading.Lock()

        def fake_process(_gh, _spec, _issue) -> None:
            with lock:
                refresh_seen_by_worker.append(refresh.call_count)

        with patch.object(workflow, "_refresh_base_and_worktrees", refresh), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=2))

        refresh.assert_called_once_with(
            gh, self._spec(parallel_limit=2), scheduler=None,
        )
        # Every worker observed refresh.call_count == 1 -- i.e. the refresh
        # completed BEFORE any `_process_issue` started.
        self.assertEqual(refresh_seen_by_worker, [1, 1])

    def test_family_aware_stages_never_overlap_with_each_other(self) -> None:
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
        import threading
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="decomposing"))
        gh.add_issue(make_issue(2, label="blocked"))
        gh.add_issue(make_issue(4, label="umbrella"))
        # An unlabeled issue routes through `_handle_pickup` -> decomposer
        # and is therefore family-aware too.
        gh.add_issue(make_issue(5, label=None))
        # A non-family-aware label that MUST fan out to a worker thread
        # AND must be allowed to overlap with the family-aware bucket.
        gh.add_issue(make_issue(99, label="implementing"))

        family_in_flight = 0
        family_max_in_flight = 0
        fanout_in_flight = 0
        # `overlap_seen` flips True if a family handler observed a fanout
        # handler in flight (or vice versa) at any moment. With workers
        # sized to fit every submission and a short sleep on each
        # handler, the family lock's `holding` handler is virtually
        # guaranteed to overlap with the (independently scheduled)
        # fanout worker. If `tick()` regressed to "drain family
        # synchronously before fanout starts" this would stay False and
        # the assertion fails.
        overlap_seen = False
        family_count = 0
        fanout_count = 0
        counter_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            nonlocal family_in_flight, family_max_in_flight
            nonlocal fanout_in_flight, overlap_seen
            nonlocal family_count, fanout_count
            family = issue.number != 99
            if family:
                with counter_lock:
                    family_in_flight += 1
                    family_max_in_flight = max(
                        family_max_in_flight, family_in_flight,
                    )
                    family_count += 1
                    if fanout_in_flight > 0:
                        overlap_seen = True
                time.sleep(0.05)
                with counter_lock:
                    family_in_flight -= 1
            else:
                with counter_lock:
                    fanout_in_flight += 1
                    fanout_count += 1
                    if family_in_flight > 0:
                        overlap_seen = True
                time.sleep(0.05)
                with counter_lock:
                    fanout_in_flight -= 1

        # parallel_limit=5 and no `global_semaphore` means every submission
        # gets its own worker thread; the family lock is the ONLY thing
        # preventing family-aware handlers from overlapping with each
        # other, and the fanout worker is free to run alongside whichever
        # family handler currently holds the lock.
        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=5))

        # Four family-aware issues observed; the family lock kept them
        # from overlapping with each other.
        self.assertEqual(family_count, 4)
        self.assertEqual(family_max_in_flight, 1)
        self.assertEqual(fanout_count, 1)
        # Fanout handler ran concurrently with at least one family
        # handler. Without the overlap fix (family draining before
        # fanout starts), `overlap_seen` would stay False.
        self.assertTrue(
            overlap_seen,
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
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="ready"))

        caller_thread = threading.get_ident()
        barrier = threading.Barrier(3, timeout=5.0)
        passed: list[tuple[int, int]] = []
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            # If the partition wrongly put `ready` in the family bucket,
            # only one of these would ever run at a time and the barrier
            # would time out (TimeoutError surfaces as test failure).
            barrier.wait()
            with lock:
                passed.append((issue.number, threading.get_ident()))

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=3))

        self.assertEqual(sorted(n for n, _ in passed), [1, 2, 3])
        # All three ran on worker threads, not the caller thread.
        for _n, tid in passed:
            self.assertNotEqual(
                tid, caller_thread,
                "ready issues must fan out to worker threads, not the caller",
            )

    def test_label_read_failure_does_not_abort_other_issues(self) -> None:
        # Per-issue exception isolation must extend to the partition's
        # label read. The reviewer's reproducer: if `gh.workflow_label`
        # raises on one issue while classifying for parallel fanout, the
        # partition loop aborts and EVERY other eligible issue this tick
        # goes unprocessed -- a regression of the existing per-issue
        # isolation invariant. The fix catches the read, logs it, and
        # routes the offending issue into the family bucket where the
        # per-issue try/except picks up any sustained failure.
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))

        original_workflow_label = FakeGitHubClient.workflow_label
        # Raise for issue #2 only; #1 and #3 return their real labels.
        def flaky_workflow_label(self, issue):
            if getattr(issue, "number", None) == 2:
                raise RuntimeError("simulated label-read failure")
            return original_workflow_label(issue)

        processed: list[int] = []
        # Issue #2 still ends up in `_process_issue` via the family
        # bucket (the partition routes label-read failures there) so the
        # fake_process gets called for it too -- but ALSO for #1 and #3,
        # proving the other issues weren't aborted.
        def fake_process(_gh, _spec, issue) -> None:
            processed.append(issue.number)

        with patch.object(FakeGitHubClient, "workflow_label", flaky_workflow_label), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=3))

        # All three issues were attempted -- the partition did not abort
        # after the bad label read on #2.
        self.assertEqual(sorted(processed), [1, 2, 3])

    def test_family_bucket_occupies_one_slot_under_tight_limit(self) -> None:
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
        import threading
        gh = FakeGitHubClient()
        # Two family-aware issues. The first is slow; the second
        # must wait for the first because the family bucket runs them
        # sequentially in one drain task.
        gh.add_issue(make_issue(1, label="decomposing"))
        gh.add_issue(make_issue(2, label="blocked"))
        # One fanout issue that MUST advance while the slow family
        # handler is still inside `_process_issue`.
        gh.add_issue(make_issue(99, label="implementing"))

        slow_family_holding = threading.Event()
        slow_family_release = threading.Event()
        fanout_done = threading.Event()
        observed_order: list[int] = []
        observed_lock = threading.Lock()
        # Errors raised in the releaser sub-thread are captured and
        # re-raised from the test thread; otherwise an AssertionError
        # inside `releaser` would only print and the test would
        # spuriously report success.
        releaser_error: list[BaseException] = []

        def fake_process(_gh, _spec, issue) -> None:
            with observed_lock:
                observed_order.append(issue.number)
            if issue.number == 1:
                slow_family_holding.set()
                # Hold until fanout completes. If the drain-task fix
                # regressed and the family bucket occupied >1 worker
                # slots, fanout would queue and `slow_family_release`
                # would only be set by the test's finally below (after
                # the join times out) -- the wait below would NOT
                # surface that directly; the releaser's assertions are
                # what actually fail.
                slow_family_release.wait(timeout=5.0)
            elif issue.number == 99:
                fanout_done.set()
            # Family issue #2 runs to completion immediately (no hold);
            # it should only run AFTER family #1 exits.

        def releaser() -> None:
            try:
                self.assertTrue(
                    slow_family_holding.wait(timeout=5.0),
                    "slow family handler never entered _process_issue",
                )
                # Crucially: fanout must complete WHILE the family
                # bucket is still mid-flight on issue #1. If the
                # family bucket occupied both worker slots, fanout
                # would be queued and `fanout_done` would never get
                # set in this window.
                self.assertTrue(
                    fanout_done.wait(timeout=5.0),
                    "fanout did not run concurrently with the slow "
                    "family handler; family bucket likely consumed "
                    "multiple slots",
                )
            except BaseException as e:  # noqa: BLE001 -- re-raised below
                releaser_error.append(e)
            finally:
                # Always release so the test thread can join cleanly
                # even when the releaser's assertions fire.
                slow_family_release.set()

        t = threading.Thread(target=releaser)
        t.start()
        try:
            # parallel_limit=2 + 3 submissions total. Family bucket =
            # one drain task = one slot. Fanout = one task = one slot.
            # The second family issue stays inside the drain task (not
            # a separate executor slot), so the fanout's slot is free
            # while issue #1 is held.
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(parallel_limit=2))
        finally:
            slow_family_release.set()
            t.join(timeout=5.0)

        if releaser_error:
            raise releaser_error[0]

        # All three issues handled.
        self.assertEqual(sorted(observed_order), [1, 2, 99])
        # Family #2 ran AFTER family #1 (drain task is sequential).
        idx_1 = observed_order.index(1)
        idx_2 = observed_order.index(2)
        self.assertLess(idx_1, idx_2, observed_order)
        # And the fanout entered `_process_issue` BEFORE family #1
        # exited (the releaser only released after `fanout_done` was
        # set, which the fanout handler sets on entry).
        idx_99 = observed_order.index(99)
        self.assertLess(idx_99, idx_2, observed_order)

    def test_slow_family_handler_does_not_block_fanout_workers(self) -> None:
        # Reviewer's reproducer: a single long decomposing / unlabeled-
        # pickup agent run must NOT block the other workers in the same
        # tick. With the family lock holding the family bucket on one
        # worker, the other (limit-1) workers must still be able to
        # advance unrelated implementing / validating issues -- otherwise
        # a mixed-stage tick collapses back to serial in practice.
        import threading
        gh = FakeGitHubClient()
        # One slow family-aware issue. The handler holds inside
        # `_process_issue` until released by the test; without the
        # overlap fix this would freeze every other worker.
        gh.add_issue(make_issue(1, label="decomposing"))
        # Several fanout issues that MUST advance while the family
        # handler is still running.
        for n in (10, 11, 12):
            gh.add_issue(make_issue(n, label="implementing"))

        family_holding = threading.Event()
        family_release = threading.Event()
        fanout_done: list[int] = []
        fanout_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            if issue.number == 1:
                family_holding.set()
                # Hold the family lock until the test confirms fanout
                # workers all finished. Time-bounded so a regression
                # surfaces as an assertion rather than a hang.
                self.assertTrue(
                    family_release.wait(timeout=5.0),
                    "family handler released by timeout, not by test",
                )
                return
            # Fanout handler.
            with fanout_lock:
                fanout_done.append(issue.number)

        def releaser() -> None:
            self.assertTrue(
                family_holding.wait(timeout=5.0),
                "family handler never entered _process_issue",
            )
            # Wait until every fanout handler completed BEFORE letting
            # the family handler exit. If fanout was blocked by the
            # family lock, this loop would time out.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with fanout_lock:
                    if len(fanout_done) == 3:
                        break
                time.sleep(0.01)
            family_release.set()

        t = threading.Thread(target=releaser)
        t.start()
        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(parallel_limit=4))
        finally:
            family_release.set()
            t.join(timeout=5.0)

        # All three fanout issues completed while the family handler
        # was still inside `_process_issue` -- exactly the property the
        # reviewer asked for. Without the overlap fix, this list would
        # be empty (or only one entry, the lucky fanout that grabbed
        # the caller thread).
        self.assertEqual(sorted(fanout_done), [10, 11, 12])

    def test_concurrent_decomposing_and_blocked_do_not_race_child_state(
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
        gh.add_issue(make_issue(10, label="decomposing"))
        gh.add_issue(make_issue(20, label="blocked"))
        gh.seed_state(
            10,
            expected_children_count=1,
            children=[20],
            umbrella=None,
        )

        # Bare-bones substitute for `_process_issue` that exercises just
        # the cross-issue write path the bug lives in. The real handlers
        # call into worktree / agent code that needs more scaffolding;
        # this distilled version reproduces the data-race scenario
        # exactly: parent reads child state, sets fields, writes back;
        # child reads its own state, parks on missing parent_number.

        def fake_process(client, _spec, issue) -> None:
            if issue.number == 10:
                # Parent's repair branch: read each recorded child,
                # set parent_number, clear park flags, write back.
                state = client.read_pinned_state(issue)
                for child_n in state.get("children") or []:
                    child = client.get_issue(int(child_n))
                    cs = client.read_pinned_state(child)
                    if not cs.get("parent_number"):
                        cs.set("parent_number", issue.number)
                        cs.set("awaiting_human", False)
                        cs.set("park_reason", None)
                        client.write_pinned_state(child, cs)
                client.set_workflow_label(issue, "blocked")
                client.write_pinned_state(issue, state)
                return
            if issue.number == 20:
                cs = client.read_pinned_state(issue)
                if cs.get("parent_number"):
                    return
                if cs.get("awaiting_human"):
                    return
                cs.set("awaiting_human", True)
                cs.set("park_reason", "blocked_no_children")
                client.write_pinned_state(issue, cs)
                return

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=4))

        # Child's final state retains the parent's seed and is not parked.
        # The family lock guarantees the two handlers ran sequentially
        # in some order; either order produces this final state because
        # the parent's repair either runs first (child sees parent_number
        # set and returns early) or last (parent's write is final).
        child_state = gh.pinned_data(20)
        self.assertEqual(child_state.get("parent_number"), 10)
        self.assertFalse(child_state.get("awaiting_human"))
        self.assertIsNone(child_state.get("park_reason"))

    def test_no_eligible_issues_is_a_noop(self) -> None:
        # An empty pollable list must not spin up worker threads or raise.
        gh = FakeGitHubClient()
        from unittest.mock import MagicMock
        process = MagicMock()
        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", process):
            workflow.tick(gh, self._spec(parallel_limit=4))
        process.assert_not_called()

    def test_global_semaphore_clamps_concurrent_in_flight(self) -> None:
        # The `global_semaphore` parameter is the host-wide ceiling threaded
        # in by `main._run_tick`. It must clamp concurrent `_process_issue`
        # calls regardless of how high `spec.parallel_limit` was
        # configured: a spec with parallel_limit=4 plus a semaphore sized
        # 2 must never have more than 2 issues in flight at once, even
        # though the per-repo executor admits 4 worker threads.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3, 4):
            gh.add_issue(make_issue(n, label="implementing"))
        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()
        admitted = threading.Semaphore(0)
        release = threading.Event()

        def fake_process(_gh, _spec, _issue) -> None:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            admitted.release()
            release.wait(timeout=5.0)
            with lock:
                in_flight -= 1

        def release_when_two_admitted() -> None:
            for _ in range(2):
                self.assertTrue(
                    admitted.acquire(timeout=5.0),
                    "fake_process never admitted 2 workers within timeout",
                )
            time.sleep(0.1)
            release.set()

        releaser = threading.Thread(target=release_when_two_admitted)
        releaser.start()
        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(
                    gh,
                    self._spec(parallel_limit=4),
                    global_semaphore=threading.BoundedSemaphore(2),
                )
        finally:
            release.set()
            releaser.join(timeout=5.0)

        # Even though parallel_limit=4 would otherwise let 4 issues run in
        # parallel, the semaphore cap of 2 must hold.
        self.assertEqual(max_in_flight, 2)

    def test_global_semaphore_size_one_serializes_processing(self) -> None:
        # With a size-1 semaphore the `_process_issue` calls must run one
        # at a time regardless of `parallel_limit`. This is the workflow-
        # level guarantee that backs `MAX_PARALLEL_ISSUES_GLOBAL=1`: even
        # with multiple worker threads spun up, only one is ever inside
        # `_process_issue`.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))
        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()

        def fake_process(_gh, _spec, _issue) -> None:
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.02)
            with lock:
                in_flight -= 1

        with patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(
                gh,
                self._spec(parallel_limit=5),
                global_semaphore=threading.BoundedSemaphore(1),
            )

        self.assertEqual(max_in_flight, 1)

    def test_parallel_path_uses_per_worker_clients_and_refetches_issues(
        self,
    ) -> None:
        # PyGithub's `Requester` is not documented thread-safe; sharing a
        # single client across worker threads can interleave concurrent
        # request setup. The parallel path must therefore (a) call
        # `gh._for_worker_thread()` once per submitted issue so each
        # worker gets its own client, and (b) refetch the Issue via the
        # WORKER'S client so the Issue's parent requester chain matches
        # the thread that actually drives it.
        import threading
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))

        # Each `_for_worker_thread()` call mints a distinct client object,
        # so a workflow regression that reused the parent client across
        # threads would fail the `is`-identity check below.
        cloned_clients: list[FakeGitHubClient] = []
        clone_lock = threading.Lock()

        def fake_clone() -> FakeGitHubClient:
            twin = FakeGitHubClient()
            # Mirror the parent's issues so `get_issue` on the worker
            # client resolves against the same issue numbers the test
            # seeded.
            for n in (1, 2, 3):
                twin.add_issue(make_issue(n, label="implementing"))
            with clone_lock:
                cloned_clients.append(twin)
            return twin

        seen: list[tuple[int, int]] = []  # (issue_number, id(worker_gh))
        get_issue_calls: list[tuple[int, int]] = []
        seen_lock = threading.Lock()

        original_get_issue = FakeGitHubClient.get_issue

        def tracking_get_issue(self, number):
            with seen_lock:
                get_issue_calls.append((number, id(self)))
            return original_get_issue(self, number)

        def fake_process(worker_gh, _spec, issue) -> None:
            with seen_lock:
                seen.append((issue.number, id(worker_gh)))

        with patch.object(gh, "_for_worker_thread", fake_clone), \
             patch.object(FakeGitHubClient, "get_issue", tracking_get_issue), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=3))

        # Every submitted issue produced exactly one worker-client clone.
        self.assertEqual(len(cloned_clients), 3)
        # Every worker client is a fresh object (no two share identity).
        self.assertEqual(len({id(c) for c in cloned_clients}), 3)
        # The parent client is NOT one of the worker clients: tick must
        # not hand the shared parent to any worker.
        self.assertNotIn(id(gh), {id(c) for c in cloned_clients})
        # Each worker called `get_issue` on its OWN client (not the parent),
        # so the refetch resolves against that client's Requester.
        parent_id = id(gh)
        for _number, client_id in get_issue_calls:
            self.assertNotEqual(client_id, parent_id)
        # And each `_process_issue` invocation saw an issue paired with the
        # same worker client that fetched it (no cross-thread Issue handoff).
        for issue_number, process_client_id in seen:
            fetch_clients = [
                cid for n, cid in get_issue_calls if n == issue_number
            ]
            self.assertIn(process_client_id, fetch_clients)
        self.assertEqual(sorted(n for n, _ in seen), [1, 2, 3])

    def test_limit_one_does_not_clone_per_issue(self) -> None:
        # Sequential mode runs on the caller thread; the PyGithub thread
        # safety rationale does not apply, so the legacy path must not
        # call `_for_worker_thread()` (avoids an unnecessary token + repo
        # round-trip for every issue on every tick).
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))
        clone = MagicMock(side_effect=lambda: self.fail(
            "_for_worker_thread must not be called on the sequential path"
        ))
        with patch.object(gh, "_for_worker_thread", clone), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue"):
            workflow.tick(gh, self._spec(parallel_limit=1))
        clone.assert_not_called()

    def test_limit_one_streams_and_processes_pre_failure_issues(self) -> None:
        # Legacy invariant: with parallel_limit=1, the loop iterates the
        # generator directly so any issue yielded BEFORE an enumeration
        # failure (PyGithub pagination error, closed-issue sweep raise) is
        # still processed. Materializing the iterator upfront would lose
        # those already-yielded issues. Generator-style fake raises
        # mid-iteration to pin the streaming contract down.
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))

        def flaky_list_pollable_issues():
            yield gh.get_issue(1)
            yield gh.get_issue(2)
            raise RuntimeError("simulated pagination failure")

        processed: list[int] = []

        def fake_process(_gh, _spec, issue) -> None:
            processed.append(issue.number)

        with patch.object(gh, "list_pollable_issues", flaky_list_pollable_issues), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            # The enumeration failure is not caught inside `tick` (it lives
            # at the per-repo boundary in `main._run_tick`), but the issues
            # yielded BEFORE the raise must still have been processed.
            with self.assertRaises(RuntimeError):
                workflow.tick(gh, self._spec(parallel_limit=1))

        self.assertEqual(processed, [1, 2])


if __name__ == "__main__":
    unittest.main()
