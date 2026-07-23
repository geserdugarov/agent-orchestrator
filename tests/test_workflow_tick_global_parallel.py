# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Global workflow tick concurrency tests."""
from __future__ import annotations

import unittest

import threading
from functools import partial
from unittest.mock import MagicMock, patch

from orchestrator import workflow

from tests import workflow_tick_parallel_test_support as support
from tests import workflow_tick_probe_test_support as probes


class TickGlobalSchedulingTest(unittest.TestCase):
    """Host limits and worker-client isolation apply across repo fanout."""

    def test_no_eligible_issues_is_a_noop(self) -> None:
        # An empty pollable list must not spin up worker threads or raise.
        gh = support.FakeGitHubClient()
        process = MagicMock()
        with patch.object(workflow, support.REFRESH_BASE), \
             patch.object(workflow, support.PROCESS_ISSUE, process):
            workflow.tick(gh, support._spec(parallel_limit=4))
        process.assert_not_called()

    def test_global_semaphore_clamps_concurrency(self) -> None:
        # The `global_semaphore` parameter is the host-wide ceiling threaded
        # in by `main._run_tick`. It must clamp concurrent `_process_issue`
        # calls regardless of how high `spec.parallel_limit` was
        # configured: a spec with parallel_limit=4 plus a semaphore sized
        # 2 must never have more than 2 issues in flight at once, even
        # though the per-repo executor admits 4 worker threads.
        gh = support.FakeGitHubClient()
        support._seed_issues(gh, (1, 2, 3, 4))
        probe = probes._BlockingConcurrencyProbe()
        with support._running_thread(
            partial(probe.release_after, 2),
            probe.cleanup,
        ), patch.object(workflow, support.REFRESH_BASE), patch.object(
            workflow,
            support.PROCESS_ISSUE,
            side_effect=probe,
        ):
            workflow.tick(
                gh,
                support._spec(parallel_limit=4),
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
        gh = support.FakeGitHubClient()
        support._seed_issues(gh, (1, 2, 3))
        probe = probes._ConcurrencyProbe(delay=support._SERIAL_PROBE_DELAY_SECONDS)

        with patch.object(workflow, support.REFRESH_BASE), \
             patch.object(workflow, support.PROCESS_ISSUE, side_effect=probe):
            workflow.tick(
                gh,
                support._spec(parallel_limit=5),
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
        scenario = support._WorkerClientScenario()

        with (
            patch.object(
                scenario.parent,
                "_for_worker_thread",
                side_effect=scenario.clone_client,
            ),
            patch.object(workflow, support.REFRESH_BASE),
            patch.object(
                workflow,
                support.PROCESS_ISSUE,
                side_effect=scenario.process_issue,
            ),
        ):
            workflow.tick(scenario.parent, support._spec(parallel_limit=3))

        scenario.assert_distinct_worker_clients(self)
        scenario.assert_worker_refetches(self)

    def test_limit_one_does_not_clone_per_issue(self) -> None:
        # Sequential mode runs on the caller thread; the PyGithub thread
        # safety rationale does not apply, so the legacy path must not
        # call `_for_worker_thread()` (avoids an unnecessary token + repo
        # round-trip for every issue on every tick).
        gh = support.FakeGitHubClient()
        support._seed_issues(gh, (1, 2, 3))
        clone = MagicMock(side_effect=AssertionError(
            "_for_worker_thread must not be called on the sequential path",
        ))
        with patch.object(gh, "_for_worker_thread", clone), \
             patch.object(workflow, support.REFRESH_BASE), \
             patch.object(workflow, support.PROCESS_ISSUE):
            workflow.tick(gh, support._spec(parallel_limit=1))
        clone.assert_not_called()

    def test_limit_one_processes_issues_before_error(self) -> None:
        # Legacy invariant: with parallel_limit=1, the loop iterates the
        # generator directly so any issue yielded BEFORE an enumeration
        # failure (PyGithub pagination error, closed-issue sweep raise) is
        # still processed. Materializing the iterator upfront would lose
        # those already-yielded issues. Generator-style fake raises
        # mid-iteration to pin the streaming contract down.
        gh = support.FakeGitHubClient()
        support._seed_issues(gh, (1, 2, 3))
        recorder = probes._IssueProcessRecorder()

        with (
            patch.object(
                gh,
                "list_pollable_issues",
                partial(support._poll_then_raise, gh),
            ),
            patch.object(workflow, support.REFRESH_BASE),
            patch.object(workflow, support.PROCESS_ISSUE, side_effect=recorder),
        ):
            # The enumeration failure is not caught inside `tick` (it lives
            # at the per-repo boundary in `main._run_tick`), but the issues
            # yielded BEFORE the raise must still have been processed.
            with self.assertRaises(RuntimeError):
                workflow.tick(gh, support._spec(parallel_limit=1))

        self.assertEqual(recorder.processed, [1, 2])
