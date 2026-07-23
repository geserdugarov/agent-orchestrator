# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import patch

from orchestrator import workflow

from tests.fakes import FakeGitHubClient, FakeLabel, make_issue
from tests.workflow_helpers import (
    LABEL_BLOCKED,
    LABEL_DECOMPOSING,
    LABEL_IMPLEMENTING,
)

from tests.scheduler_routing_workers import (
    _GatedWorker,
    _wait_for_first_started,
    _wait_for_log,
)

from tests.scheduler_routing_test_support import (
    _SchedulerWorkflowTest,
)

REPO_SLUG = "acme/widget"
TARGET_ROOT = Path("/tmp/orchestrator-test-target-root")
PROCESS_ISSUE = "_process_issue"
REFRESH_BASE = "_refresh_base_and_worktrees"
FANOUT_START_TIMEOUT_MESSAGE = "implementing fanout #1 did not start"
POLL_INTERVAL_SECONDS = 0.01
EVENT_TIMEOUT_SECONDS = 2.0
WORKER_TIMEOUT_SECONDS = 5.0
DEFERRED_ISSUE_NUMBERS = (10, 11, 12)
FAMILY_ISSUE_NUMBER = 42
RELABELLED_FANOUT_ISSUE_NUMBER = 50


def _assert_drain_stalls(test_case, client, scheduler, process) -> None:
    workflow.tick(client, test_case._spec(), scheduler=scheduler)
    test_case.assertTrue(
        process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS),
        "drain did not enter the first family-aware issue",
    )
    time.sleep(0.1)
    test_case.assertFalse(process.starts[2].is_set())
    test_case.assertEqual(process.maximum_in_flight, 1)
    workflow.tick(client, test_case._spec(), scheduler=scheduler)
    time.sleep(0.1)
    test_case.assertFalse(process.starts[2].is_set())
    test_case.assertEqual(process.maximum_in_flight, 1)


def _release_ordered_drain(test_case, process) -> None:
    process.releases[1].set()
    test_case.assertTrue(
        process.starts[2].wait(timeout=EVENT_TIMEOUT_SECONDS),
        "drain did not advance after the first release",
    )
    process.releases[2].set()


def _assert_drain_complete(test_case, scheduler, process) -> None:
    test_case._wait_idle(scheduler)
    test_case.assertEqual(process.maximum_in_flight, 1)
    test_case.assertEqual(sorted(process.processed_snapshot()), [1, 2])


def _start_sequential_pair(test_case, client, scheduler, process) -> int:
    workflow.tick(client, test_case._spec(), scheduler=scheduler)
    started_first = _wait_for_first_started(process.starts)
    test_case.assertIsNotNone(started_first)
    time.sleep(0.1)
    return started_first


def _release_sequential_pair(test_case, process, started_first: int) -> None:
    second = 2 if started_first == 1 else 1
    test_case.assertFalse(process.starts[second].is_set())
    process.releases[started_first].set()
    test_case.assertTrue(
        process.starts[second].wait(timeout=EVENT_TIMEOUT_SECONDS),
        "second family issue did not run after the first release",
    )
    process.releases[second].set()


def _family_client(*labels: str | None) -> FakeGitHubClient:
    client = FakeGitHubClient()
    for issue_number, label in enumerate(labels, start=1):
        client.add_issue(make_issue(issue_number, label=label))
    return client


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
        gh = _family_client(LABEL_DECOMPOSING, LABEL_BLOCKED)

        process = self._sequential_processor(1, 2)
        with self._route_through(process):
            _assert_drain_stalls(self, gh, sched, process)
            _release_ordered_drain(self, process)
        _assert_drain_complete(self, sched, process)

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
        with self._route_through(process):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS))

            with self.assertLogs(
                "orchestrator.workflow",
                level=logging.INFO,
            ) as logs:
                workflow.tick(gh, self._spec(), scheduler=sched)
                log_output = logs.output
            self.assertTrue(
                any("family bucket" in message and "not submitted" in message for message in log_output),
                log_output,
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
        gh.add_issue(make_issue(FAMILY_ISSUE_NUMBER, label=LABEL_DECOMPOSING))

        process = self._processor(FAMILY_ISSUE_NUMBER)
        with self._route_through(process):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(
                process.starts[FAMILY_ISSUE_NUMBER].wait(
                    timeout=EVENT_TIMEOUT_SECONDS,
                )
            )
            self.assertTrue(sched.is_active(REPO_SLUG, FAMILY_ISSUE_NUMBER))
        process.release_all()
        self._wait_idle(sched)
        # After completion, #42's per-iteration claim is released.
        self.assertFalse(sched.is_active(REPO_SLUG, FAMILY_ISSUE_NUMBER))

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
        gh.add_issue(
            make_issue(
                RELABELLED_FANOUT_ISSUE_NUMBER,
                label=LABEL_IMPLEMENTING,
            )
        )

        # Simulate the fanout worker holding (acme/widget, 50) via a
        # direct scheduler.submit that parks until released.
        fanout = _GatedWorker()
        self.addCleanup(fanout.release.set)
        process = self._processor(
            RELABELLED_FANOUT_ISSUE_NUMBER,
            blocking=False,
        )

        self.assertTrue(
            sched.submit(REPO_SLUG, RELABELLED_FANOUT_ISSUE_NUMBER, fanout),
        )
        self.assertTrue(fanout.started.wait(timeout=EVENT_TIMEOUT_SECONDS))

        # Relabel #50 to a family-aware state so the next tick
        # folds it into the family bucket.
        gh._issues[RELABELLED_FANOUT_ISSUE_NUMBER].labels = [
            FakeLabel(LABEL_BLOCKED),
        ]

        with (
            self.assertLogs(
                "orchestrator.workflow",
                level=logging.INFO,
            ) as logs,
            patch.object(workflow, REFRESH_BASE),
            patch.object(workflow, PROCESS_ISSUE, side_effect=process),
        ):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(
                _wait_for_log(logs, "already in flight", "#50"),
                logs.output,
            )
        self.assertNotIn(
            RELABELLED_FANOUT_ISSUE_NUMBER,
            process.processed_snapshot(),
        )
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
        gh = _family_client(LABEL_DECOMPOSING, None)

        process = self._sequential_processor(1, 2)
        with self._route_through(process):
            started_first = _start_sequential_pair(self, gh, sched, process)
            _release_sequential_pair(self, process, started_first)
        _assert_drain_complete(self, sched, process)
