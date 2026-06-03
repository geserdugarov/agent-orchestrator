# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import base_sync, config, workflow

from tests.fakes import FakeGitHubClient, FakeLabel, make_issue


class TickViaSchedulerTest(unittest.TestCase):
    """`workflow.tick` accepts an optional `IssueScheduler` that takes
    over per-issue dispatch entirely: each polling pass enumerates the
    pollable issues, classifies family-aware vs fan-out work, and
    submits a per-issue callable to the scheduler. The submit path is
    nonblocking -- a duplicate active issue, a per-repo or global cap
    hit, or a family slot already held is simply skipped this tick and
    a later polling pass retries against the live scheduler state.

    Tests use a real `IssueScheduler` (not a mock) so the in-flight
    state across multiple polling passes is the same state the
    production scheduler would expose, and they gate workers with
    `threading.Event` so concurrency is observable without sleep-and-
    pray timing.
    """

    def _spec(self, parallel_limit: int = 5) -> config.RepoSpec:
        return config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/orchestrator-test-target-root"),
            base_branch="main",
            parallel_limit=parallel_limit,
        )

    def _wait_idle(self, sched, repo_slug: str, deadline_s: float = 5.0) -> None:
        """Block until the scheduler reports zero active workers for `repo_slug`.

        The done-callback releases the in-flight markers from a background
        thread, so a brief poll prevents the next tick from observing the
        old marker. Time-bounded so a regression fails the test instead of
        hanging the suite.
        """
        import threading as _threading
        deadline = _threading.Event()
        timer = _threading.Timer(deadline_s, deadline.set)
        timer.daemon = True
        timer.start()
        try:
            while sched.active_count(repo_slug) > 0 and not deadline.is_set():
                pass
        finally:
            timer.cancel()
        self.assertEqual(
            sched.active_count(repo_slug), 0,
            f"scheduler still has active workers on {repo_slug}",
        )

    def test_active_issue_is_skipped_until_completion(self) -> None:
        # Tick 1 accepts the issue and the worker holds inside
        # `_process_issue`. Tick 2 must NOT submit the same issue
        # again while it is still in flight -- the scheduler's
        # duplicate-active-issue gate keeps a second submit out so the
        # handler isn't entered twice concurrently. After the worker
        # exits, a third tick may submit it again.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(7, label="implementing"))

        start = threading.Event()
        release = threading.Event()
        call_count = 0
        count_lock = threading.Lock()

        def fake_process(_gh, _spec, _issue) -> None:
            nonlocal call_count
            with count_lock:
                call_count += 1
            start.set()
            release.wait(timeout=5.0)

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                # Tick 1: accept and dispatch.
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(
                    start.wait(timeout=2.0),
                    "worker never entered _process_issue after first tick",
                )
                self.assertTrue(sched.is_active("acme/widget", 7))

                # Tick 2: while the worker is still in flight, the
                # duplicate-active gate must reject the resubmit. The
                # handler must NOT be called a second time.
                workflow.tick(gh, self._spec(), scheduler=sched)
                # Brief breathing room: any in-flight executor task
                # would have invoked the fake by now.
                time.sleep(0.1)
                with count_lock:
                    self.assertEqual(call_count, 1)
                self.assertTrue(sched.is_active("acme/widget", 7))

                # Release the worker and let it complete.
                release.set()
            self._wait_idle(sched, "acme/widget")

            # Tick 3: completion cleared the marker so the same issue
            # is accepted again.
            second_start = threading.Event()

            def fake_process_2(_gh, _spec, _issue) -> None:
                nonlocal call_count
                with count_lock:
                    call_count += 1
                second_start.set()

            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process_2):
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(
                    second_start.wait(timeout=2.0),
                    "worker never re-entered _process_issue after first completed",
                )
        finally:
            release.set()

        with count_lock:
            self.assertEqual(call_count, 2)

    def test_same_repo_fanout_proceeds_when_limits_allow(self) -> None:
        # Three non-family issues on the same repo with the scheduler's
        # per-repo cap set wide enough to admit all three. The dispatch
        # loop must submit each one and the scheduler must let all three
        # workers run concurrently -- the per-repo cap is the only gate
        # that could keep them apart.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=3)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label="implementing"))

        barrier = threading.Barrier(3, timeout=5.0)
        passed: list[int] = []
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            # If any submission was rejected, fewer than three workers
            # would arrive at the barrier and `wait` would raise
            # BrokenBarrierError on timeout -- the test then fails on
            # the unrejected workers' unhandled exception.
            barrier.wait()
            with lock:
                passed.append(issue.number)

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(), scheduler=sched)
                # Wait for all three to pass through the barrier and
                # record their issue numbers. The barrier guarantees
                # they're all in `_process_issue` at the same time;
                # this loop just waits for the final list write.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    with lock:
                        if len(passed) == 3:
                            break
                    time.sleep(0.01)
            self._wait_idle(sched, "acme/widget")
        finally:
            # Defensive: a barrier broken by an early failure could
            # leave a worker pinned; releasing the underlying scheduler
            # is enough because `addCleanup(sched.shutdown)` waits for
            # workers to exit.
            pass

        self.assertEqual(sorted(passed), [1, 2, 3])

    def test_per_repo_cap_skips_overflow_until_a_slot_frees(self) -> None:
        # With `parallel_limit=2` and three eligible non-family issues,
        # the first two are accepted and the third is skipped this
        # tick. After one of the in-flight workers exits, a follow-up
        # tick admits the previously-skipped issue.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        for n in (10, 11, 12):
            gh.add_issue(make_issue(n, label="implementing"))

        starts: dict[int, threading.Event] = {
            10: threading.Event(),
            11: threading.Event(),
            12: threading.Event(),
        }
        releases: dict[int, threading.Event] = {
            10: threading.Event(),
            11: threading.Event(),
            12: threading.Event(),
        }
        seen: list[int] = []
        seen_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            starts[issue.number].set()
            with seen_lock:
                seen.append(issue.number)
            releases[issue.number].wait(timeout=5.0)

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                # Tick 1: parallel_limit=2 caps to two accepted submits.
                workflow.tick(
                    gh, self._spec(parallel_limit=2), scheduler=sched,
                )
                # Two workers must enter; the third must NOT (per-repo
                # cap holds it back).
                accepted = [
                    n for n, ev in starts.items() if ev.wait(timeout=2.0)
                ]
                self.assertEqual(len(accepted), 2, accepted)
                time.sleep(0.1)
                rejected_numbers = [n for n in (10, 11, 12) if n not in accepted]
                self.assertEqual(len(rejected_numbers), 1)
                rejected_number = rejected_numbers[0]
                self.assertFalse(
                    starts[rejected_number].is_set(),
                    f"#{rejected_number} should have been skipped by per-repo cap",
                )

                # Release one of the two accepted workers and wait for
                # the scheduler to reflect the freed slot.
                drained = accepted[0]
                releases[drained].set()
                deadline = threading.Event()
                timer = threading.Timer(2.0, deadline.set)
                timer.daemon = True
                timer.start()
                try:
                    while (
                        sched.is_active("acme/widget", drained)
                        and not deadline.is_set()
                    ):
                        pass
                finally:
                    timer.cancel()
                self.assertFalse(sched.is_active("acme/widget", drained))

                # The handler stub does not flip labels, so close the
                # FakeIssue directly to model "advanced past this
                # stage" -- in production the drained worker would
                # have relabeled / closed the issue and the next
                # enumeration would skip it. Without this, the next
                # tick would re-admit the drained issue and take the
                # newly freed slot back, starving the previously-
                # skipped one.
                gh._issues[drained].closed = True

                # Tick 2: previously-skipped issue is now admitted.
                workflow.tick(
                    gh, self._spec(parallel_limit=2), scheduler=sched,
                )
                self.assertTrue(
                    starts[rejected_number].wait(timeout=2.0),
                    f"#{rejected_number} not admitted after a slot freed up",
                )
        finally:
            for ev in releases.values():
                ev.set()

        # All three issues eventually ran exactly once between the two ticks.
        self.assertEqual(sorted(seen), [10, 11, 12])

    def test_family_aware_drains_sequentially_within_one_bucket(self) -> None:
        # All family-aware issues this tick are folded into ONE bucket
        # task that drains them sequentially. The bucket holds the family
        # slot for the whole drain so a concurrent tick mid-drain cannot
        # squeeze a second family worker past the gate, and at no point
        # do two family-aware handlers run concurrently. Crucially, the
        # drain advances to the next family issue within the SAME tick's
        # bucket task -- no extra polling pass needed -- which is the
        # issue #326 fix: a backlog/blocked child can no longer take the
        # family slot and starve the parent umbrella issue.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="decomposing"))
        gh.add_issue(make_issue(2, label="blocked"))

        family_in_flight = 0
        family_max_in_flight = 0
        family_starts: dict[int, threading.Event] = {
            1: threading.Event(),
            2: threading.Event(),
        }
        family_releases: dict[int, threading.Event] = {
            1: threading.Event(),
            2: threading.Event(),
        }
        order: list[int] = []
        counter_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            nonlocal family_in_flight, family_max_in_flight
            with counter_lock:
                family_in_flight += 1
                family_max_in_flight = max(
                    family_max_in_flight, family_in_flight,
                )
                order.append(issue.number)
            family_starts[issue.number].set()
            family_releases[issue.number].wait(timeout=5.0)
            with counter_lock:
                family_in_flight -= 1

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                # Tick 1: one bucket task submitted, drain enters its
                # first family-aware issue.
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(
                    family_starts[1].wait(timeout=2.0),
                    "drain did not enter the first family-aware issue",
                )
                time.sleep(0.1)
                self.assertFalse(
                    family_starts[2].is_set(),
                    "drain entered second family-aware issue before "
                    "releasing the first -- bucket must process "
                    "sequentially",
                )
                with counter_lock:
                    self.assertEqual(family_in_flight, 1)

                # Tick 2 BEFORE releasing the first handler: the family
                # slot is still held by the bucket task, so a second
                # bucket submit must be skipped. This is the "do not
                # overlap across polling passes" property: a polling
                # pass that observes a family worker mid-flight cannot
                # squeeze a second one past the gate.
                workflow.tick(gh, self._spec(), scheduler=sched)
                time.sleep(0.1)
                self.assertFalse(
                    family_starts[2].is_set(),
                    "family-slot leak: second family worker started "
                    "while the first was still in flight",
                )
                with counter_lock:
                    self.assertEqual(family_in_flight, 1)

                # Release #1. The SAME bucket task advances to #2
                # without needing another tick -- that's the bug-fix
                # contract: a no-op family child cannot block the next
                # family issue (e.g. the parent umbrella) from running.
                family_releases[1].set()
                self.assertTrue(
                    family_starts[2].wait(timeout=2.0),
                    "drain did not advance to second family issue "
                    "after first one was released",
                )
                family_releases[2].set()
            self._wait_idle(sched, "acme/widget")
        finally:
            for ev in family_releases.values():
                ev.set()

        # At no point did two family-aware handlers run concurrently.
        self.assertEqual(family_max_in_flight, 1)
        # Both issues ran exactly once -- and within ticks 1's bucket.
        self.assertEqual(sorted(order), [1, 2])

    def test_family_bucket_skip_is_logged(self) -> None:
        # The dispatch layer logs a "family bucket (...) not submitted
        # this tick" line when the previous tick's bucket is still
        # draining, so an operator can correlate "umbrella not
        # advancing" with the slot still being held. The underlying
        # scheduler also logs the per-submit `reason=family_slot_held`
        # skip; this test asserts the higher-level dispatch context
        # makes it into the log too.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="decomposing"))
        gh.add_issue(make_issue(2, label="blocked"))

        start = threading.Event()
        release = threading.Event()
        entered: list[int] = []
        lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            with lock:
                entered.append(issue.number)
            start.set()
            release.wait(timeout=5.0)

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(start.wait(timeout=2.0))

                # The drain is parked on the first family issue; a
                # follow-up tick must observe the bucket skip and log
                # it with the count of pending family issues.
                with self.assertLogs(
                    "orchestrator.workflow", level=logging.INFO,
                ) as cm:
                    workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(
                    any(
                        "family bucket" in msg and "not submitted" in msg
                        for msg in cm.output
                    ),
                    cm.output,
                )
        finally:
            release.set()
        self._wait_idle(sched, "acme/widget")

    def test_family_drain_marks_in_progress_issue_as_active(self) -> None:
        # The bucket task wraps each per-issue iteration in
        # `scheduler.track_active` so `is_active(repo, n)` reports True
        # for the issue currently being processed inside the bucket.
        # Without this, the pre-tick base refresh would not skip the
        # in-flight family issue's worktree and could race the agent.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(42, label="decomposing"))

        start = threading.Event()
        release = threading.Event()

        def fake_process(_gh, _spec, _issue) -> None:
            start.set()
            release.wait(timeout=5.0)

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(start.wait(timeout=2.0))
                # The bucket's sentinel key (issue 0) IS active and the
                # currently-processed family issue #42 is ALSO marked
                # active so the refresh-skip contract holds.
                self.assertTrue(sched.is_active("acme/widget", 42))
        finally:
            release.set()
        self._wait_idle(sched, "acme/widget")
        # After completion, #42's per-iteration claim is released.
        self.assertFalse(sched.is_active("acme/widget", 42))

    def test_family_drain_skips_issue_already_in_flight(self) -> None:
        # Cross-tick race: tick N classifies #50 as fanout (e.g.
        # `implementing`) and submits it. Before that worker finishes,
        # something relabels #50 into a family-aware state and tick N+1
        # folds it into the family bucket. The bucket's drain reaches
        # #50, sees `track_active` cannot claim (fanout worker still
        # holds the active marker), and must SKIP `_process_issue` for
        # that iteration -- two workers running the same handler
        # concurrently would race the worktree and pinned state.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(50, label="implementing"))

        # Simulate the fanout worker holding (acme/widget, 50) via a
        # direct scheduler.submit that parks until released.
        fanout_start = threading.Event()
        fanout_release = threading.Event()

        def _fanout_worker() -> None:
            fanout_start.set()
            fanout_release.wait(timeout=5.0)

        process_calls: list[int] = []
        process_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            with process_lock:
                process_calls.append(issue.number)

        try:
            # Plant the fanout worker on #50.
            self.assertTrue(
                sched.submit("acme/widget", 50, _fanout_worker),
            )
            self.assertTrue(fanout_start.wait(timeout=2.0))

            # Relabel #50 to a family-aware state so the next tick
            # folds it into the family bucket.
            gh._issues[50].labels = [FakeLabel("blocked")]

            with self.assertLogs(
                "orchestrator.workflow", level=logging.INFO,
            ) as cm, \
                 patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(), scheduler=sched)
                # Wait for the bucket drain to attempt #50 and skip it.
                deadline = time.monotonic() + 2.0
                skipped = False
                while time.monotonic() < deadline and not skipped:
                    skipped = any(
                        "already in flight" in m and "#50" in m
                        for m in cm.output
                    )
                    time.sleep(0.01)
                self.assertTrue(skipped, cm.output)
            # The fanout worker is the ONLY one that processed #50;
            # the drain refused to enter a second concurrent handler.
            with process_lock:
                self.assertNotIn(50, process_calls)
        finally:
            fanout_release.set()
        self._wait_idle(sched, "acme/widget")

    def test_unlabeled_pickup_is_treated_as_family_aware(self) -> None:
        # An unlabeled issue routes through `_handle_pickup`, which can
        # create children and seed their pinned state -- a cross-issue
        # write, same as decomposing/blocked/umbrella. Dispatch must
        # therefore fold it into the family bucket alongside the
        # explicit family labels and process it sequentially under the
        # one family slot, never as a fanout submit.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=8, per_repo_cap=8)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="decomposing"))
        gh.add_issue(make_issue(2, label=None))

        family_in_flight = 0
        family_max_in_flight = 0
        starts: dict[int, threading.Event] = {
            1: threading.Event(),
            2: threading.Event(),
        }
        releases: dict[int, threading.Event] = {
            1: threading.Event(),
            2: threading.Event(),
        }
        order: list[int] = []
        counter_lock = threading.Lock()

        def fake_process(_gh, _spec, issue) -> None:
            nonlocal family_in_flight, family_max_in_flight
            with counter_lock:
                family_in_flight += 1
                family_max_in_flight = max(
                    family_max_in_flight, family_in_flight,
                )
                order.append(issue.number)
            starts[issue.number].set()
            releases[issue.number].wait(timeout=5.0)
            with counter_lock:
                family_in_flight -= 1

        try:
            with patch.object(workflow, "_refresh_base_and_worktrees"), \
                 patch.object(workflow, "_process_issue", side_effect=fake_process):
                workflow.tick(gh, self._spec(), scheduler=sched)
                # Drain enters its first family-aware issue (could be
                # either depending on enumeration order). The other
                # must NOT be entered until the first is released --
                # the bucket drains sequentially.
                started_first = None
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and started_first is None:
                    for n, ev in starts.items():
                        if ev.is_set():
                            started_first = n
                            break
                    time.sleep(0.01)
                self.assertIsNotNone(started_first)
                time.sleep(0.1)
                second = 2 if started_first == 1 else 1
                self.assertFalse(
                    starts[second].is_set(),
                    "second family-aware issue must wait for the first "
                    "to release inside the drain",
                )

                # Release the first; the SAME bucket task advances to
                # the second family-aware issue.
                releases[started_first].set()
                self.assertTrue(
                    starts[second].wait(timeout=2.0),
                    "unlabeled-pickup issue did not run inside the "
                    "family bucket after the first family issue released",
                )
                releases[second].set()
        finally:
            for ev in releases.values():
                ev.set()
        self._wait_idle(sched, "acme/widget")

        # Both ran exactly once, sequentially, in the same bucket.
        self.assertEqual(family_max_in_flight, 1)
        self.assertEqual(sorted(order), [1, 2])

    def test_legacy_path_used_when_scheduler_is_none(self) -> None:
        # `scheduler=None` must keep the existing synchronous in-thread
        # behavior intact. The legacy path runs `_process_issue` on the
        # caller thread for `parallel_limit=1`, never touches the
        # scheduler, and -- crucially -- never calls `_for_worker_thread`
        # on that path.
        import threading
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))

        caller_thread = threading.get_ident()
        worker_threads: list[int] = []

        def fake_process(_gh, _spec, _issue) -> None:
            worker_threads.append(threading.get_ident())

        clone = MagicMock(side_effect=lambda: self.fail(
            "_for_worker_thread must not be called on the legacy path"
        ))
        with patch.object(gh, "_for_worker_thread", clone), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(parallel_limit=1))

        self.assertEqual(worker_threads, [caller_thread])
        clone.assert_not_called()

    def test_refresh_skips_active_issue_on_next_tick(self) -> None:
        # The "active issues are skipped until completion" requirement
        # has to hold for the pre-tick base refresh too, not just the
        # scheduler.submit gate. The refresh iterates per-issue
        # worktrees and either rebases (pre-PR) or relabels /
        # state-mutates (PR-having); racing that against a still-
        # running handler corrupts the worktree under the agent or
        # clobbers pinned state mid-write.
        #
        # Drive two ticks: tick 1 dispatches the issue and the worker
        # holds inside `_process_issue`. Tick 2 calls the refresh
        # helper -- but because the scheduler reports the issue as
        # active, the refresh must skip its per-worktree sync. This
        # test inspects how `_refresh_base_and_worktrees` (the real
        # one, not a mock) treats the active-issue case by patching
        # only the inner `_sync_worktree_with_base` step, which is
        # what would actually mutate the worktree / pinned state.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(7, label="implementing"))

        start = threading.Event()
        release = threading.Event()

        def fake_process(_gh, _spec, _issue) -> None:
            start.set()
            release.wait(timeout=5.0)

        # Stub fetch + iterdir so the real `_refresh_base_and_worktrees`
        # runs but never touches the filesystem or the network. The
        # scheduler-aware skip lives in the per-worktree loop; if it
        # regressed, `sync` would be called for the still-active
        # issue.
        sync_calls: list[int] = []
        sync_lock = threading.Lock()

        def fake_sync(_gh, _spec, _wt, issue_number) -> None:
            with sync_lock:
                sync_calls.append(issue_number)

        class _FakeWtDir:
            def __init__(self, name: str) -> None:
                self.name = name
            def is_dir(self) -> bool:
                return True

        fake_fetch_result = type("R", (), {"returncode": 0, "stderr": ""})()
        fake_root = type(
            "Root", (),
            {
                "exists": lambda self: True,
                "iterdir": lambda self: [_FakeWtDir("issue-7")],
            },
        )()

        try:
            with patch.object(
                base_sync, "_authed_target_fetch",
                return_value=fake_fetch_result,
            ), patch.object(
                base_sync, "_repo_worktrees_root", return_value=fake_root,
            ), patch.object(
                base_sync, "_sync_worktree_with_base", side_effect=fake_sync,
            ), patch.object(workflow, "_process_issue", side_effect=fake_process):
                # Tick 1: handler is dispatched and parks in fake_process.
                workflow.tick(gh, self._spec(), scheduler=sched)
                self.assertTrue(
                    start.wait(timeout=2.0),
                    "worker never entered _process_issue",
                )
                self.assertTrue(sched.is_active("acme/widget", 7))
                # The first tick's refresh ran while issue #7 was NOT
                # yet active in the scheduler (the worker is dispatched
                # AFTER refresh completes), so `_sync_worktree_with_base`
                # may or may not have been called depending on ordering.
                # Reset the call log before the second tick so the
                # assertion below isolates the "active issue skip"
                # property.
                with sync_lock:
                    sync_calls.clear()

                # Tick 2: scheduler still reports #7 as active. The
                # refresh helper must observe that and skip the
                # per-worktree sync entirely.
                workflow.tick(gh, self._spec(), scheduler=sched)
                with sync_lock:
                    self.assertEqual(
                        sync_calls, [],
                        "refresh did not skip active issue's worktree; "
                        "_sync_worktree_with_base was called for an "
                        "in-flight handler",
                    )

                # Release the worker, wait for the slot to clear, and
                # confirm a follow-up tick DOES sync the (now idle)
                # worktree -- the skip is conditional on active state,
                # not a permanent suppression.
                release.set()
            self._wait_idle(sched, "acme/widget")

            with patch.object(
                base_sync, "_authed_target_fetch",
                return_value=fake_fetch_result,
            ), patch.object(
                base_sync, "_repo_worktrees_root", return_value=fake_root,
            ), patch.object(
                base_sync, "_sync_worktree_with_base", side_effect=fake_sync,
            ), patch.object(workflow, "_process_issue"):
                workflow.tick(gh, self._spec(), scheduler=sched)
                with sync_lock:
                    self.assertIn(7, sync_calls)
        finally:
            release.set()

    def test_scheduler_path_uses_per_worker_client_and_refetches_issue(
        self,
    ) -> None:
        # The scheduler dispatch must mirror the legacy parallel path:
        # mint a worker-thread client via `_for_worker_thread()` and
        # refetch the Issue against that client so PyGithub's
        # Requester chain isn't shared across threads.
        import threading
        from orchestrator.scheduler import IssueScheduler
        sched = IssueScheduler(global_cap=4, per_repo_cap=4)
        self.addCleanup(sched.shutdown)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))

        clone_calls: list[int] = []
        clone_lock = threading.Lock()
        cloned_clients: list[FakeGitHubClient] = []

        def fake_clone() -> FakeGitHubClient:
            twin = FakeGitHubClient()
            twin.add_issue(make_issue(1, label="implementing"))
            with clone_lock:
                clone_calls.append(1)
                cloned_clients.append(twin)
            return twin

        seen_client_ids: list[int] = []

        def fake_process(worker_gh, _spec, _issue) -> None:
            seen_client_ids.append(id(worker_gh))

        with patch.object(gh, "_for_worker_thread", fake_clone), \
             patch.object(workflow, "_refresh_base_and_worktrees"), \
             patch.object(workflow, "_process_issue", side_effect=fake_process):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self._wait_idle(sched, "acme/widget")

        self.assertEqual(len(cloned_clients), 1)
        # The parent client is NOT what the worker saw.
        self.assertNotIn(id(gh), seen_client_ids)
        self.assertEqual(seen_client_ids[0], id(cloned_clients[0]))



if __name__ == "__main__":
    unittest.main()
