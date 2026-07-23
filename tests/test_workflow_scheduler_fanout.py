# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from orchestrator import workflow

from tests.fakes import FakeGitHubClient, FakeLabel, make_issue
from tests.workflow_helpers import (
    LABEL_IMPLEMENTING,
)

from tests.scheduler_routing_workers import (
    _BarrierIssueProcessor,
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


@dataclass(frozen=True)
class _FanoutScenario:
    scheduler: object
    client: FakeGitHubClient
    process: object


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
        scheduler = self._scheduler(global_cap=4, per_repo_cap=4)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(7, label=LABEL_IMPLEMENTING))

        first_process = self._processor(7)
        self._hold_across_duplicate_tick(gh, scheduler, first_process)

        second_process = self._processor(7, blocking=False)
        with self._route_through(second_process):
            workflow.tick(gh, self._spec(), scheduler=scheduler)
            self.assertTrue(
                second_process.starts[7].wait(timeout=EVENT_TIMEOUT_SECONDS),
                "worker never re-entered _process_issue after first completed",
            )
        self.assertEqual(
            first_process.processed_snapshot() + second_process.processed_snapshot(),
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
        for issue_number in (1, 2, 3):
            gh.add_issue(make_issue(issue_number, label=LABEL_IMPLEMENTING))

        process = _BarrierIssueProcessor(parties=3)

        with self._route_through(process):
            workflow.tick(gh, self._spec(), scheduler=sched)
            deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if len(process.processed_snapshot()) == 3:
                    break
                time.sleep(POLL_INTERVAL_SECONDS)
        self._wait_idle(sched)

        self.assertEqual(sorted(process.processed_snapshot()), [1, 2, 3])

    def test_repo_cap_defers_until_slot_frees(self) -> None:
        # With `parallel_limit=2` and three eligible non-family issues,
        # the first two are accepted and the third is skipped this
        # tick. After one of the in-flight workers exits, a follow-up
        # tick admits the previously-skipped issue.
        scenario = self._fanout_scenario()
        with self._route_through(scenario.process):
            workflow.tick(
                scenario.client,
                self._spec(parallel_limit=2),
                scheduler=scenario.scheduler,
            )
            accepted, rejected = self._assert_initial_cap(scenario.process)
            self._free_slot_and_tick(scenario, accepted[0], rejected)

        # All three issues eventually ran exactly once between the two ticks.
        self.assertEqual(
            sorted(scenario.process.processed_snapshot()),
            [10, 11, 12],
        )

    def _hold_across_duplicate_tick(
        self,
        client: FakeGitHubClient,
        scheduler,
        process,
    ) -> None:
        with self._route_through(process):
            workflow.tick(client, self._spec(), scheduler=scheduler)
            self.assertTrue(
                process.starts[7].wait(timeout=EVENT_TIMEOUT_SECONDS),
                "worker never entered _process_issue after first tick",
            )
            workflow.tick(client, self._spec(), scheduler=scheduler)
            time.sleep(0.1)
            self.assertEqual(process.processed_snapshot(), [7])
            self.assertTrue(scheduler.is_active(REPO_SLUG, 7))
            process.releases[7].set()
        self._wait_idle(scheduler)

    def _fanout_scenario(self) -> _FanoutScenario:
        client = FakeGitHubClient()
        for issue_number in DEFERRED_ISSUE_NUMBERS:
            client.add_issue(
                make_issue(issue_number, label=LABEL_IMPLEMENTING),
            )
        return _FanoutScenario(
            scheduler=self._scheduler(),
            client=client,
            process=self._processor(*DEFERRED_ISSUE_NUMBERS),
        )

    def _assert_initial_cap(self, process) -> tuple[list[int], int]:
        accepted = []
        for number, started in process.starts.items():
            if started.wait(timeout=EVENT_TIMEOUT_SECONDS):
                accepted.append(number)
        self.assertEqual(len(accepted), 2, accepted)
        time.sleep(0.1)
        rejected = next(candidate for candidate in DEFERRED_ISSUE_NUMBERS if candidate not in accepted)
        self.assertFalse(process.starts[rejected].is_set())
        return accepted, rejected

    def _free_slot_and_tick(
        self,
        scenario: _FanoutScenario,
        drained: int,
        rejected: int,
    ) -> None:
        scenario.process.releases[drained].set()
        self._wait_issue_idle(scenario.scheduler, drained)
        scenario.client._issues[drained].closed = True
        scenario.client._issues[drained].labels = [FakeLabel("done")]
        workflow.tick(
            scenario.client,
            self._spec(parallel_limit=2),
            scheduler=scenario.scheduler,
        )
        self.assertTrue(
            scenario.process.starts[rejected].wait(
                timeout=EVENT_TIMEOUT_SECONDS,
            ),
            f"#{rejected} not admitted after a slot freed up",
        )
