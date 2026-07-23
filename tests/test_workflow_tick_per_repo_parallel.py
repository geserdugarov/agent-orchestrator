# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-repository workflow tick concurrency tests."""
from __future__ import annotations

import unittest

import threading
from functools import partial
from unittest.mock import MagicMock, patch

from orchestrator import workflow

from tests import workflow_tick_parallel_test_support as support
from tests import workflow_tick_probe_test_support as probes


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
        gh = support.FakeGitHubClient()
        support._seed_issues(gh, (1, 2, 3))
        caller_thread = threading.get_ident()
        probe = probes._ConcurrencyProbe()

        with patch.object(workflow, support.REFRESH_BASE), \
             patch.object(workflow, support.PROCESS_ISSUE, side_effect=probe):
            workflow.tick(gh, support._spec(parallel_limit=1))

        self.assertEqual(probe.max_in_flight, 1)
        self.assertEqual(probe.order, [1, 2, 3])
        self.assertEqual(probe.thread_ids, {caller_thread})

    def test_limit_caps_concurrent_in_flight(self) -> None:
        # With parallel_limit=2 and 4 eligible issues, the executor must
        # admit at most 2 simultaneously. A blocking fake holds each thread
        # until released so we can observe the steady-state concurrency.
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
            workflow.tick(gh, support._spec(parallel_limit=2))

        self.assertTrue(probe.admissions_complete)
        self.assertEqual(probe.max_in_flight, 2)

    def test_limit_allows_full_concurrency_up_to_cap(self) -> None:
        # With parallel_limit=3 and 3 eligible issues, ALL three must be
        # able to run concurrently. A `threading.Barrier(3)` synchronizes
        # the three workers: if only fewer-than-cap were admitted the
        # barrier would block forever and the test would time out. The
        # bounded `wait` makes that failure mode surface as an assertion.
        gh = support.FakeGitHubClient()
        support._seed_issues(gh, (1, 2, 3))
        recorder = probes._BarrierProcessRecorder(3)

        with patch.object(workflow, support.REFRESH_BASE), \
             patch.object(workflow, support.PROCESS_ISSUE, side_effect=recorder):
            workflow.tick(gh, support._spec(parallel_limit=3))

        self.assertEqual(sorted(recorder.records), [1, 2, 3])

    def test_failing_issue_does_not_stop_other_issues(self) -> None:
        # The exception isolation invariant must hold under the parallel
        # path too: one raising issue must not prevent the other eligible
        # issues from completing.
        gh = support.FakeGitHubClient()
        support._seed_issues(gh, (1, 2, 3))
        recorder = probes._IssueProcessRecorder(failing_issue=2)

        with patch.object(workflow, support.REFRESH_BASE), \
             patch.object(workflow, support.PROCESS_ISSUE, side_effect=recorder):
            workflow.tick(gh, support._spec(parallel_limit=3))

        self.assertEqual(sorted(recorder.processed), [1, 3])

    def test_refresh_runs_once_before_parallel_fanout(self) -> None:
        # The pre-tick base refresh must still happen exactly once per
        # tick, before any issue handler runs, even on the parallel path.
        # Otherwise concurrent worktree fanout could race the still-stale
        # base SHA into the per-issue merges.
        gh = support.FakeGitHubClient()
        support._seed_issues(gh, (1, 2))
        refresh = MagicMock()
        recorder = probes._RefreshOrderRecorder(refresh)

        with patch.object(workflow, support.REFRESH_BASE, refresh), \
             patch.object(workflow, support.PROCESS_ISSUE, side_effect=recorder):
            workflow.tick(gh, support._spec(parallel_limit=2))

        refresh.assert_called_once_with(
            gh, support._spec(parallel_limit=2), scheduler=None,
        )
        # Every worker observed refresh.call_count == 1 -- i.e. the refresh
        # completed BEFORE any `_process_issue` started.
        self.assertEqual(recorder.calls, [1, 1])
