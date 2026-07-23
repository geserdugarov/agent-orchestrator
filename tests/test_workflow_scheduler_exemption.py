# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import time
from pathlib import Path

from orchestrator import workflow

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    LABEL_BLOCKED,
    LABEL_DECOMPOSING,
    LABEL_IMPLEMENTING,
    LABEL_UMBRELLA,
)

from tests.scheduler_routing_workers import (
    _wait_for_first_started,
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
        with self._route_through(process):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(
                process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS),
                FANOUT_START_TIMEOUT_MESSAGE,
            )
            self.assertTrue(
                process.starts[2].wait(timeout=EVENT_TIMEOUT_SECONDS),
                "umbrella #2 was blocked by the per-repo cap -- the exempt bucket must run alongside the fanout slot",
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
        with self._route_through(process):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS))
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
        for issue_number, label in (
            (1, LABEL_DECOMPOSING),
            (2, LABEL_UMBRELLA),
            (3, LABEL_IMPLEMENTING),
        ):
            gh.add_issue(make_issue(issue_number, label=label))

        process = self._processor(1, 2, 3)
        with self._route_through(process):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS))
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
        with self._route_through(process):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(
                process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS),
                FANOUT_START_TIMEOUT_MESSAGE,
            )
            self.assertTrue(
                process.starts[2].wait(timeout=EVENT_TIMEOUT_SECONDS),
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
        with self._route_through(process):
            workflow.tick(gh, self._spec(), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS))
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
        for issue_number, label in (
            (1, LABEL_BLOCKED),
            (2, LABEL_DECOMPOSING),
            (3, LABEL_IMPLEMENTING),
        ):
            gh.add_issue(make_issue(issue_number, label=label))

        process = self._processor(1, 2, 3)
        with self._route_through(process):
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
