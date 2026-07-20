# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import base_sync, config, workflow
from orchestrator.github import BACKLOG_LABEL, PAUSED_LABEL
from orchestrator.scheduler import IssueScheduler

from tests.fakes import FakeGitHubClient, FakeLabel, make_issue
from tests.workflow_helpers import (
    LABEL_BLOCKED,
    LABEL_DECOMPOSING,
    LABEL_IMPLEMENTING,
    LABEL_IN_REVIEW,
    LABEL_UMBRELLA,
    LABEL_VALIDATING,
    STATE_CLOSED,
    STATE_OPEN,
    TEST_BASE_BRANCH,
)

REPO_SLUG = "acme/widget"
TARGET_ROOT = Path("/tmp/orchestrator-test-target-root")
PROCESS_ISSUE = "_process_issue"
REFRESH_BASE = "_refresh_base_and_worktrees"
FANOUT_START_TIMEOUT_MESSAGE = "implementing fanout #1 did not start"


def _wait_for_first_started(
    starts: dict[int, threading.Event],
    *,
    timeout: float = 2.0,
) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for issue_number, started in starts.items():
            if started.is_set():
                return issue_number
        time.sleep(0.01)
    return None


def _record_current_thread(thread_ids: list[int], _gh, _spec, _issue) -> None:
    thread_ids.append(threading.get_ident())


def _wait_for_log(log_capture, *fragments: str, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(
            all(fragment in message for fragment in fragments)
            for message in log_capture.output
        ):
            return True
        time.sleep(0.01)
    return False


class _IssueProcessor:
    def __init__(self, issue_numbers: tuple[int, ...], *, blocking: bool = True):
        self.starts = {
            issue_number: threading.Event() for issue_number in issue_numbers
        }
        self.releases = {
            issue_number: threading.Event() for issue_number in issue_numbers
        }
        self.processed: list[int] = []
        self._blocking = blocking
        self._lock = threading.Lock()

    def __call__(self, _gh, _spec, issue) -> None:
        with self._lock:
            self.processed.append(issue.number)
        start = self.starts.get(issue.number)
        if start is not None:
            start.set()
        if self._blocking:
            release = self.releases.get(issue.number)
            if release is not None:
                release.wait(timeout=5.0)

    def release_all(self) -> None:
        for release in self.releases.values():
            release.set()

    def processed_snapshot(self) -> list[int]:
        with self._lock:
            return list(self.processed)


class _GatedWorker:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> None:
        self.started.set()
        self.release.wait(timeout=5.0)


class _SequentialIssueProcessor(_IssueProcessor):
    def __init__(self, issue_numbers: tuple[int, ...]):
        super().__init__(issue_numbers)
        self.maximum_in_flight = 0
        self._in_flight = 0

    def __call__(self, gh, spec, issue) -> None:
        with self._lock:
            self._in_flight += 1
            self.maximum_in_flight = max(
                self.maximum_in_flight,
                self._in_flight,
            )
        try:
            super().__call__(gh, spec, issue)
        finally:
            with self._lock:
                self._in_flight -= 1


class _BarrierIssueProcessor:
    def __init__(self, parties: int):
        self._barrier = threading.Barrier(parties, timeout=5.0)
        self._processed: list[int] = []
        self._lock = threading.Lock()

    def __call__(self, _gh, _spec, issue) -> None:
        self._barrier.wait()
        with self._lock:
            self._processed.append(issue.number)

    def processed_snapshot(self) -> list[int]:
        with self._lock:
            return list(self._processed)


class _WorkerClientFactory:
    def __init__(self) -> None:
        self.clients: list[FakeGitHubClient] = []
        self._lock = threading.Lock()

    def __call__(self) -> FakeGitHubClient:
        client = FakeGitHubClient()
        client.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))
        with self._lock:
            self.clients.append(client)
        return client


class _FakeWorktreeDir:
    name = "issue-7"

    def is_dir(self) -> bool:
        return True


class _FakeWorktreeRoot:
    def exists(self) -> bool:
        return True

    def iterdir(self) -> list[_FakeWorktreeDir]:
        return [_FakeWorktreeDir()]


class _PyGithubIssue:
    def __init__(self, state: str):
        self.state = state


class _SchedulerWorkflowTest(unittest.TestCase):
    def _spec(self, parallel_limit: int = 5) -> config.RepoSpec:
        return config.RepoSpec(
            slug=REPO_SLUG,
            target_root=TARGET_ROOT,
            base_branch=TEST_BASE_BRANCH,
            parallel_limit=parallel_limit,
        )

    def _scheduler(
        self,
        *,
        global_cap: int = 8,
        per_repo_cap: int = 8,
    ) -> IssueScheduler:
        scheduler = IssueScheduler(
            global_cap=global_cap,
            per_repo_cap=per_repo_cap,
        )
        self.addCleanup(scheduler.shutdown)
        return scheduler

    def _processor(
        self,
        *issue_numbers: int,
        blocking: bool = True,
    ) -> _IssueProcessor:
        processor = _IssueProcessor(issue_numbers, blocking=blocking)
        self.addCleanup(processor.release_all)
        return processor

    def _sequential_processor(
        self,
        *issue_numbers: int,
    ) -> _SequentialIssueProcessor:
        processor = _SequentialIssueProcessor(issue_numbers)
        self.addCleanup(processor.release_all)
        return processor

    def _wait_idle(
        self,
        scheduler: IssueScheduler,
        repo_slug: str = REPO_SLUG,
        deadline_s: float = 5.0,
    ) -> None:
        deadline = time.monotonic() + deadline_s
        while scheduler.active_count(repo_slug) > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(
            scheduler.active_count(repo_slug),
            0,
            f"scheduler still has active workers on {repo_slug}",
        )

    def _wait_issue_idle(
        self,
        scheduler: IssueScheduler,
        issue_number: int,
        *,
        timeout: float = 2.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while (
            scheduler.is_active(REPO_SLUG, issue_number)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertFalse(scheduler.is_active(REPO_SLUG, issue_number))


class TickFanoutRoutingTest(_SchedulerWorkflowTest):
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

    def test_active_issue_is_skipped_until_completion(self) -> None:
        # Tick 1 accepts the issue and the worker holds inside
        # `_process_issue`. Tick 2 must NOT submit the same issue
        # again while it is still in flight -- the scheduler's
        # duplicate-active-issue gate keeps a second submit out so the
        # handler isn't entered twice concurrently. After the worker
        # exits, a third tick may submit it again.
        sched = self._scheduler(global_cap=4, per_repo_cap=4)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(7, label=LABEL_IMPLEMENTING))

        first_process = self._processor(7)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=first_process,
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(
                first_process.starts[7].wait(timeout=2.0),
                "worker never entered _process_issue after first tick",
            )
            self.assertTrue(sched.is_active(REPO_SLUG, 7))

            workflow.tick(gh, self._spec(), scheduler=sched)
            time.sleep(0.1)
            self.assertEqual(first_process.processed_snapshot(), [7])
            self.assertTrue(sched.is_active(REPO_SLUG, 7))
            first_process.releases[7].set()
        self._wait_idle(sched)

        second_process = self._processor(7, blocking=False)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=second_process,
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(
                second_process.starts[7].wait(timeout=2.0),
                "worker never re-entered _process_issue after first completed",
            )
        self.assertEqual(
            first_process.processed_snapshot()
            + second_process.processed_snapshot(),
            [7, 7],
        )

    def test_same_repo_fanout_runs_with_capacity(self) -> None:
        # Three non-family issues on the same repo with the scheduler's
        # per-repo cap set wide enough to admit all three. The dispatch
        # loop must submit each one and the scheduler must let all three
        # workers run concurrently -- the per-repo cap is the only gate
        # that could keep them apart.
        sched = self._scheduler(global_cap=8, per_repo_cap=3)
        gh = FakeGitHubClient()
        for n in (1, 2, 3):
            gh.add_issue(make_issue(n, label=LABEL_IMPLEMENTING))

        process = _BarrierIssueProcessor(parties=3)

        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if len(process.processed_snapshot()) == 3:
                    break
                time.sleep(0.01)
        self._wait_idle(sched)

        self.assertEqual(sorted(process.processed_snapshot()), [1, 2, 3])

    def test_repo_cap_defers_until_slot_frees(self) -> None:
        # With `parallel_limit=2` and three eligible non-family issues,
        # the first two are accepted and the third is skipped this
        # tick. After one of the in-flight workers exits, a follow-up
        # tick admits the previously-skipped issue.
        sched = self._scheduler()
        gh = FakeGitHubClient()
        for n in (10, 11, 12):
            gh.add_issue(make_issue(n, label=LABEL_IMPLEMENTING))

        process = self._processor(10, 11, 12)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(
                gh, self._spec(parallel_limit=2), scheduler=sched,
            )
            accepted = [
                number
                for number, started in process.starts.items()
                if started.wait(timeout=2.0)
            ]
            self.assertEqual(len(accepted), 2, accepted)
            time.sleep(0.1)
            rejected_numbers = [
                number for number in (10, 11, 12) if number not in accepted
            ]
            self.assertEqual(len(rejected_numbers), 1)
            rejected_number = rejected_numbers[0]
            self.assertFalse(
                process.starts[rejected_number].is_set(),
                f"#{rejected_number} should have been skipped by per-repo cap",
            )

            drained = accepted[0]
            process.releases[drained].set()
            self._wait_issue_idle(sched, drained)

            gh._issues[drained].closed = True
            gh._issues[drained].labels = [FakeLabel("done")]
            workflow.tick(
                gh, self._spec(parallel_limit=2), scheduler=sched,
            )
            self.assertTrue(
                process.starts[rejected_number].wait(timeout=2.0),
                f"#{rejected_number} not admitted after a slot freed up",
            )

        # All three issues eventually ran exactly once between the two ticks.
        self.assertEqual(sorted(process.processed_snapshot()), [10, 11, 12])


class FamilyBucketRoutingTest(_SchedulerWorkflowTest):
    def test_family_bucket_drains_in_order(self) -> None:
        # All family-aware issues this tick are folded into ONE bucket
        # task that drains them sequentially. The bucket holds the family
        # slot for the whole drain so a concurrent tick mid-drain cannot
        # squeeze a second family worker past the gate, and at no point
        # do two family-aware handlers run concurrently. Crucially, the
        # drain advances to the next family issue within the SAME tick's
        # bucket task -- no extra polling pass needed -- which is the
        # issue #326 fix: a backlog/blocked child can no longer take the
        # family slot and starve the parent umbrella issue.
        sched = self._scheduler()
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_DECOMPOSING))
        gh.add_issue(make_issue(2, label=LABEL_BLOCKED))

        process = self._sequential_processor(1, 2)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(
                process.starts[1].wait(timeout=2.0),
                "drain did not enter the first family-aware issue",
            )
            time.sleep(0.1)
            self.assertFalse(
                process.starts[2].is_set(),
                "drain entered second family-aware issue before "
                "releasing the first -- bucket must process sequentially",
            )
            self.assertEqual(process.maximum_in_flight, 1)

            workflow.tick(gh, self._spec(), scheduler=sched)
            time.sleep(0.1)
            self.assertFalse(
                process.starts[2].is_set(),
                "family-slot leak: second family worker started "
                "while the first was still in flight",
            )
            self.assertEqual(process.maximum_in_flight, 1)

            process.releases[1].set()
            self.assertTrue(
                process.starts[2].wait(timeout=2.0),
                "drain did not advance to second family issue "
                "after first one was released",
            )
            process.releases[2].set()
        self._wait_idle(sched)

        # At no point did two family-aware handlers run concurrently.
        self.assertEqual(process.maximum_in_flight, 1)
        # Both issues ran exactly once -- and within ticks 1's bucket.
        self.assertEqual(sorted(process.processed_snapshot()), [1, 2])

    def test_family_bucket_skip_is_logged(self) -> None:
        # The dispatch layer logs a "family bucket (...) not submitted
        # this tick" line when the previous tick's bucket is still
        # draining, so an operator can correlate "umbrella not
        # advancing" with the slot still being held. The underlying
        # scheduler also logs the per-submit `reason=family_slot_held`
        # skip; this test asserts the higher-level dispatch context
        # makes it into the log too.
        sched = self._scheduler()
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_DECOMPOSING))
        gh.add_issue(make_issue(2, label=LABEL_BLOCKED))

        process = self._processor(1, 2)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=2.0))

            with self.assertLogs(
                "orchestrator.workflow", level=logging.INFO,
            ) as logs:
                workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(
                any(
                    "family bucket" in message and "not submitted" in message
                    for message in logs.output
                ),
                logs.output,
            )
        process.release_all()
        self._wait_idle(sched)

    def test_family_drain_marks_issue_active(self) -> None:
        # The bucket task wraps each per-issue iteration in
        # `scheduler.track_active` so `is_active(repo, n)` reports True
        # for the issue currently being processed inside the bucket.
        # Without this, the pre-tick base refresh would not skip the
        # in-flight family issue's worktree and could race the agent.
        sched = self._scheduler()
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(42, label=LABEL_DECOMPOSING))

        process = self._processor(42)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(process.starts[42].wait(timeout=2.0))
            self.assertTrue(sched.is_active(REPO_SLUG, 42))
        process.release_all()
        self._wait_idle(sched)
        # After completion, #42's per-iteration claim is released.
        self.assertFalse(sched.is_active(REPO_SLUG, 42))

    def test_family_drain_skips_active_issue(self) -> None:
        # Cross-tick race: tick N classifies #50 as fanout (e.g.
        # `implementing`) and submits it. Before that worker finishes,
        # something relabels #50 into a family-aware state and tick N+1
        # folds it into the family bucket. The bucket's drain reaches
        # #50, sees `track_active` cannot claim (fanout worker still
        # holds the active marker), and must SKIP `_process_issue` for
        # that iteration -- two workers running the same handler
        # concurrently would race the worktree and pinned state.
        sched = self._scheduler()
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(50, label=LABEL_IMPLEMENTING))

        # Simulate the fanout worker holding (acme/widget, 50) via a
        # direct scheduler.submit that parks until released.
        fanout = _GatedWorker()
        self.addCleanup(fanout.release.set)
        process = self._processor(50, blocking=False)

        self.assertTrue(
            sched.submit(REPO_SLUG, 50, fanout),
        )
        self.assertTrue(fanout.started.wait(timeout=2.0))

        # Relabel #50 to a family-aware state so the next tick
        # folds it into the family bucket.
        gh._issues[50].labels = [FakeLabel(LABEL_BLOCKED)]

        with (
            self.assertLogs(
                "orchestrator.workflow", level=logging.INFO,
            ) as logs,
            patch.object(workflow, REFRESH_BASE),
            patch.object(workflow, PROCESS_ISSUE, side_effect=process),
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(
                _wait_for_log(logs, "already in flight", "#50"),
                logs.output,
            )
        self.assertNotIn(50, process.processed_snapshot())
        fanout.release.set()
        self._wait_idle(sched)

    def test_unlabeled_pickup_is_family_aware(self) -> None:
        # An unlabeled issue routes through `_handle_pickup`, which can
        # create children and seed their pinned state -- a cross-issue
        # write, same as decomposing/blocked/umbrella. Dispatch must
        # therefore fold it into the family bucket alongside the
        # explicit family labels and process it sequentially under the
        # one family slot, never as a fanout submit.
        sched = self._scheduler()
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_DECOMPOSING))
        gh.add_issue(make_issue(2, label=None))

        process = self._sequential_processor(1, 2)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            started_first = _wait_for_first_started(process.starts)
            self.assertIsNotNone(started_first)
            time.sleep(0.1)
            second = 2 if started_first == 1 else 1
            self.assertFalse(
                process.starts[second].is_set(),
                "second family-aware issue must wait for the first "
                "to release inside the drain",
            )

            process.releases[started_first].set()
            self.assertTrue(
                process.starts[second].wait(timeout=2.0),
                "unlabeled-pickup issue did not run inside the "
                "family bucket after the first family issue released",
            )
            process.releases[second].set()
        self._wait_idle(sched)

        # Both ran exactly once, sequentially, in the same bucket.
        self.assertEqual(process.maximum_in_flight, 1)
        self.assertEqual(sorted(process.processed_snapshot()), [1, 2])


class TickExecutionIsolationTest(_SchedulerWorkflowTest):
    def test_legacy_path_used_when_scheduler_is_none(self) -> None:
        # `scheduler=None` must keep the existing synchronous in-thread
        # behavior intact. The legacy path runs `_process_issue` on the
        # caller thread for `parallel_limit=1`, never touches the
        # scheduler, and -- crucially -- never calls `_for_worker_thread`
        # on that path.
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))

        caller_thread = threading.get_ident()
        worker_threads: list[int] = []

        clone = MagicMock(side_effect=AssertionError(
            "_for_worker_thread must not be called on the legacy path",
        ))
        with patch.object(gh, "_for_worker_thread", clone), patch.object(
            workflow,
            REFRESH_BASE,
        ), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=lambda *args: _record_current_thread(worker_threads, *args),
        ):
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
        sched = self._scheduler(global_cap=4, per_repo_cap=4)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(7, label=LABEL_IMPLEMENTING))

        process = self._processor(7)

        # Stub fetch + iterdir so the real `_refresh_base_and_worktrees`
        # runs but never touches the filesystem or the network. The
        # scheduler-aware skip lives in the per-worktree loop; if it
        # regressed, `sync` would be called for the still-active
        # issue.
        sync = MagicMock()
        fake_fetch_result = MagicMock(returncode=0, stderr="")
        fake_root = _FakeWorktreeRoot()

        with patch.object(
            base_sync, "_authed_target_fetch",
            return_value=fake_fetch_result,
        ), patch.object(
            base_sync, "_repo_worktrees_root", return_value=fake_root,
        ), patch.object(
            base_sync, "_sync_worktree_with_base", sync,
        ), patch.object(workflow, PROCESS_ISSUE, side_effect=process):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(
                process.starts[7].wait(timeout=2.0),
                "worker never entered _process_issue",
            )
            self.assertTrue(sched.is_active(REPO_SLUG, 7))
            sync.reset_mock()
            workflow.tick(gh, self._spec(), scheduler=sched)
            sync.assert_not_called()
            process.releases[7].set()
        self._wait_idle(sched)

        with patch.object(
            base_sync, "_authed_target_fetch",
            return_value=fake_fetch_result,
        ), patch.object(
            base_sync, "_repo_worktrees_root", return_value=fake_root,
        ), patch.object(
            base_sync, "_sync_worktree_with_base", sync,
        ), patch.object(workflow, PROCESS_ISSUE):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertEqual(sync.call_args.args[3], 7)

    def test_workers_use_own_clients_and_refetch(
        self,
    ) -> None:
        # The scheduler dispatch must mirror the legacy parallel path:
        # mint a worker-thread client via `_for_worker_thread()` and
        # refetch the Issue against that client so PyGithub's
        # Requester chain isn't shared across threads.
        sched = self._scheduler(global_cap=4, per_repo_cap=4)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))

        client_factory = _WorkerClientFactory()
        process = MagicMock()

        with patch.object(gh, "_for_worker_thread", client_factory), patch.object(
            workflow,
            REFRESH_BASE,
        ), patch.object(workflow, PROCESS_ISSUE, process):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self._wait_idle(sched, REPO_SLUG)

        self.assertEqual(len(client_factory.clients), 1)
        # The parent client is NOT what the worker saw.
        worker_client = process.call_args.args[0]
        self.assertIsNot(worker_client, gh)
        self.assertIs(worker_client, client_factory.clients[0])


class UmbrellaCapExemptionTest(_SchedulerWorkflowTest):
    """A family bucket is cap-exempt when every issue in it this tick
    runs a no-agent / no-worktree handler -- i.e. every label is in
    ``workflow._CAP_EXEMPT_FAMILY_LABELS`` (``blocked`` or ``umbrella``,
    both pure label / dep-graph walks). Such a bucket is submitted
    ``cap_exempt=True`` so a cheap-polling parent cannot be starved by
    ordinary implementation work when the parallel caps are saturated --
    notably a ``blocked`` parent waiting on its own children, which would
    otherwise deadlock the children it blocks under the default
    ``parallel_limit=1``. Buckets containing ``decomposing`` (spawns the
    decomposer agent) or an unlabeled-pickup ``None`` stay cap-counted
    because those entries DO real, slot-worthy work.
    """

    def test_umbrella_bucket_ignores_saturated_cap(self) -> None:
        # Per-repo cap is 1 and a fanout `implementing` issue already
        # holds the slot. A pure umbrella bucket on the same repo must
        # still run this tick: the dispatcher submits it cap-exempt so
        # the parent aggregation cannot be starved by the implementer.
        sched = self._scheduler(per_repo_cap=1)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))
        gh.add_issue(make_issue(2, label=LABEL_UMBRELLA))

        process = self._processor(1, 2)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(
                process.starts[1].wait(timeout=2.0),
                FANOUT_START_TIMEOUT_MESSAGE,
            )
            self.assertTrue(
                process.starts[2].wait(timeout=2.0),
                "umbrella #2 was blocked by the per-repo cap -- the "
                "exempt bucket must run alongside the fanout slot",
            )
        process.release_all()
        self._wait_idle(sched)

    def test_umbrella_bucket_keeps_counters(self) -> None:
        # While an umbrella-only bucket is in flight, the scheduler's
        # `active_count` must report ZERO cap-counted workers: the
        # bucket sentinel lives in the cap-exempt tracked set. Without
        # this, a follow-up fanout submit on a tightly-capped repo
        # would see the umbrella inflating the counter and be skipped.
        sched = self._scheduler(global_cap=4, per_repo_cap=4)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_UMBRELLA))

        process = self._processor(1)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=2.0))
            self.assertEqual(sched.active_count(), 0)
            self.assertEqual(sched.active_count(REPO_SLUG), 0)
            self.assertTrue(sched.is_active(REPO_SLUG, 1))
        process.release_all()
        self._wait_idle(sched)

    def test_mixed_family_bucket_counts_against_caps(self) -> None:
        # When the bucket has a non-umbrella family entry (decomposing
        # here), the cap-exempt path must NOT engage -- the
        # decomposing handler invokes an agent and is real work. A
        # fanout submit beyond the per-repo cap must be skipped this
        # tick.
        sched = self._scheduler(per_repo_cap=1)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_DECOMPOSING))
        gh.add_issue(make_issue(2, label=LABEL_UMBRELLA))
        gh.add_issue(make_issue(3, label=LABEL_IMPLEMENTING))

        process = self._processor(1, 2, 3)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=2.0))
            time.sleep(0.1)
            self.assertFalse(
                process.starts[3].is_set(),
                "implementing #3 should have been rejected by the "
                "per-repo cap -- the family bucket is cap-counted "
                "because the mix contains a non-umbrella entry",
            )
            self.assertEqual(sched.active_count(REPO_SLUG), 1)
        process.release_all()
        self._wait_idle(sched)

    def test_blocked_bucket_ignores_saturated_cap(self) -> None:
        # Regression for the blocked-parent deadlock: `_handle_blocked` is
        # a pure child-poll / dep-graph walk -- no agent, no worktree --
        # exactly like umbrella. With per-repo cap 1 and a fanout child
        # already holding the slot, the `blocked` parent bucket must STILL
        # run this tick. Before the fix the bucket was cap-counted, so the
        # parent (dispatched first) grabbed the only slot every tick and
        # starved the very child it was blocked on -- deadlocking the pair.
        sched = self._scheduler(per_repo_cap=1)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))
        gh.add_issue(make_issue(2, label=LABEL_BLOCKED))

        process = self._processor(1, 2)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(
                process.starts[1].wait(timeout=2.0),
                FANOUT_START_TIMEOUT_MESSAGE,
            )
            self.assertTrue(
                process.starts[2].wait(timeout=2.0),
                "blocked #2 was starved by the per-repo cap -- the "
                "no-agent family bucket must run cap-exempt alongside "
                "the fanout slot",
            )
        process.release_all()
        self._wait_idle(sched)

    def test_blocked_bucket_keeps_counters(self) -> None:
        # A `blocked`-only bucket is in flight but the scheduler's
        # cap counters must read ZERO: the bucket sentinel lives in the
        # cap-exempt tracked set, so a follow-up fanout submit on a
        # tightly-capped repo is not blocked by the parent's poll.
        sched = self._scheduler(global_cap=4, per_repo_cap=4)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_BLOCKED))

        process = self._processor(1)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=2.0))
            self.assertEqual(sched.active_count(), 0)
            self.assertEqual(sched.active_count(REPO_SLUG), 0)
            self.assertTrue(sched.is_active(REPO_SLUG, 1))
        process.release_all()
        self._wait_idle(sched)

    def test_mixed_blocked_bucket_still_counts(self) -> None:
        # The cap-exemption requires EVERY family entry to be a no-agent
        # handler. A bucket mixing `blocked` (no agent) with `decomposing`
        # (spawns the decomposer agent) must stay cap-counted -- `blocked`
        # in the mix does not rescue the exemption. A fanout submit beyond
        # the per-repo cap is therefore skipped this tick.
        sched = self._scheduler(per_repo_cap=1)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_BLOCKED))
        gh.add_issue(make_issue(2, label=LABEL_DECOMPOSING))
        gh.add_issue(make_issue(3, label=LABEL_IMPLEMENTING))

        process = self._processor(1, 2, 3)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertIsNotNone(_wait_for_first_started(process.starts))
            time.sleep(0.1)
            self.assertFalse(
                process.starts[3].is_set(),
                "implementing #3 should be rejected by the per-repo cap "
                "-- a bucket containing decomposing stays cap-counted "
                "even with a blocked sibling",
            )
            self.assertEqual(sched.active_count(REPO_SLUG), 1)
        process.release_all()
        self._wait_idle(sched)


class _BacklogDispatchFixture(_SchedulerWorkflowTest):
    """A hard-skip (`backlog` / `paused`) issue carries no workflow label, so
    the per-tick dispatcher would otherwise fold it into the family bucket.
    Because such an issue is neither `blocked` nor `umbrella`, that flips the
    whole bucket to cap-counted -- and under `parallel_limit=1` the bucket
    then reserves the only per-repo slot every tick, starving all fanout work
    behind a parked issue. The dispatcher must drop hard-skip issues BEFORE
    the family/fanout split so they never reserve or block a scheduler slot
    (`_process_issue` skips them anyway).
    """

    def _parked_issue(self, number: int, label: str = BACKLOG_LABEL):
        issue = make_issue(number)
        issue.labels.append(FakeLabel(label))
        return issue

    def _assert_parked_does_not_starve_fanout(self, parked_label: str) -> None:
        # Per-repo cap 1: a parked hard-skip issue (no workflow label) and a
        # real `implementing` fanout issue. Left in, the parked issue forms a
        # cap-counted family bucket that grabs the only slot, so the
        # implementer is `per_repo_cap`-skipped every tick. Filtered at
        # dispatch, the fanout runs and the parked issue is never processed.
        sched = self._scheduler(per_repo_cap=1)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))
        gh.add_issue(self._parked_issue(2, parked_label))

        process = self._processor(1)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(
                process.starts[1].wait(timeout=2.0),
                f"implementing #1 was starved -- the {parked_label} issue "
                "must not occupy the only per-repo slot",
            )
        process.release_all()
        self._wait_idle(sched)
        self.assertNotIn(
            2,
            process.processed_snapshot(),
            f"{parked_label} #2 must be filtered at dispatch, never processed",
        )


class BacklogDispatchFilterTest(_BacklogDispatchFixture):
    def test_backlog_only_does_not_starve_fanout(self) -> None:
        self._assert_parked_does_not_starve_fanout(BACKLOG_LABEL)

    def test_paused_only_does_not_starve_fanout(self) -> None:
        self._assert_parked_does_not_starve_fanout(PAUSED_LABEL)

    def test_backlog_blocked_bucket_stays_exempt(self) -> None:
        # The production regression: a `blocked` parent and a parked
        # `backlog` issue share the family bucket. The backlog issue (label
        # None) used to force `cap_exempt=False`, so the bucket reserved the
        # only slot and the `implementing` fanout never ran. With the backlog
        # issue filtered out, the bucket is `blocked`-only -> cap-exempt, so
        # BOTH the blocked parent and the fanout implementer run this tick.
        sched = self._scheduler(per_repo_cap=1)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))
        gh.add_issue(make_issue(2, label=LABEL_BLOCKED))
        gh.add_issue(self._parked_issue(3, BACKLOG_LABEL))

        process = self._processor(1, 2)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(
                process.starts[1].wait(timeout=2.0),
                FANOUT_START_TIMEOUT_MESSAGE,
            )
            self.assertTrue(
                process.starts[2].wait(timeout=2.0),
                "blocked #2 did not start -- the bucket must stay "
                "cap-exempt once the backlog issue is filtered out",
            )
        process.release_all()
        self._wait_idle(sched)
        self.assertNotIn(
            3,
            process.processed_snapshot(),
            "backlog #3 must be filtered at dispatch, never processed",
        )


class ClosedFanoutCapExemptionTest(_SchedulerWorkflowTest):
    """A CLOSED fan-out issue (a merged-PR or closed-question issue still
    carrying its sweep label) only runs a terminal finalization (flip to
    `done` / `rejected` + branch cleanup) with no agent spawn, so the
    dispatcher submits it `cap_exempt=True`. It must finalize promptly even
    when an open fan-out issue holds the only per-repo slot under
    `parallel_limit=1` -- otherwise a merged-PR issue sits closed-but-
    labeled for many ticks behind a sibling validating/documenting agent.
    """

    def test_closed_fanout_runs_when_cap_saturated(self) -> None:
        # Per-repo cap is 1 and an open `validating` fanout issue holds the
        # slot. A CLOSED `in_review` issue on the same repo must still run
        # this tick: it is submitted cap-exempt so its terminal finalize
        # is not starved by the active reviewer.
        sched = self._scheduler(per_repo_cap=1)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_VALIDATING))
        closed = make_issue(2, label=LABEL_IN_REVIEW)
        closed.closed = True
        gh.add_issue(closed)

        process = self._processor(1, 2)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(
                process.starts[1].wait(timeout=2.0),
                "open validating #1 did not start",
            )
            self.assertTrue(
                process.starts[2].wait(timeout=2.0),
                "closed in_review #2 was starved by the per-repo cap -- "
                "a terminal finalization must run cap-exempt",
            )
        process.release_all()
        self._wait_idle(sched)

    def test_closed_fanout_does_not_inflate_counters(self) -> None:
        # While a closed fan-out finalize is in flight, the scheduler's
        # cap counters stay at zero (its worker lives in the cap-exempt
        # tracked set), so a concurrent open fan-out submit is not skipped.
        sched = self._scheduler(global_cap=4, per_repo_cap=4)
        gh = FakeGitHubClient()
        closed = make_issue(1, label=LABEL_IN_REVIEW)
        closed.closed = True
        gh.add_issue(closed)

        process = self._processor(1)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(parallel_limit=4), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=2.0))
            self.assertEqual(sched.active_count(), 0)
            self.assertEqual(sched.active_count(REPO_SLUG), 0)
            self.assertTrue(sched.is_active(REPO_SLUG, 1))
        process.release_all()
        self._wait_idle(sched)

    def test_open_fanout_is_not_cap_exempt(self) -> None:
        # The exemption is closed-only: an OPEN fan-out issue beyond the
        # per-repo cap is still skipped this tick (no cap-exempt smuggling).
        sched = self._scheduler(per_repo_cap=1)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_VALIDATING))
        gh.add_issue(make_issue(2, label=LABEL_IN_REVIEW))  # OPEN -> cap-counted

        process = self._processor(1, 2)
        with patch.object(workflow, REFRESH_BASE), patch.object(
            workflow,
            PROCESS_ISSUE,
            side_effect=process,
        ):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=2.0))
            self.assertFalse(
                process.starts[2].wait(timeout=1.0),
                "open in_review #2 should be cap-skipped, not exempt",
            )
        process.release_all()
        self._wait_idle(sched)


class IssueIsClosedHelperTest(unittest.TestCase):
    """`_issue_is_closed` tolerates both the PyGithub (`state`) and the
    in-memory-fake (`closed`) shapes."""

    def test_detects_fake_closed_bool(self) -> None:
        issue = make_issue(1, label=LABEL_IN_REVIEW)
        self.assertFalse(workflow._issue_is_closed(issue))
        issue.closed = True
        self.assertTrue(workflow._issue_is_closed(issue))

    def test_detects_pygithub_state_string(self) -> None:
        self.assertTrue(
            workflow._issue_is_closed(_PyGithubIssue(STATE_CLOSED)),
        )
        self.assertFalse(
            workflow._issue_is_closed(_PyGithubIssue(STATE_OPEN)),
        )


if __name__ == "__main__":
    unittest.main()
