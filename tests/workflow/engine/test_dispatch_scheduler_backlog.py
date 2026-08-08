# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from orchestrator import workflow
from orchestrator.github.labels import BACKLOG_LABEL, PAUSED_LABEL

from tests.workflow.engine.dispatch_scheduler_workers import patch_base_refresh

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    LABEL_BLOCKED,
    LABEL_IMPLEMENTING,
)

from tests.workflow.engine.dispatch_scheduler_test_support import (
    _patch_process_issue,
    _BacklogDispatchFixture,
)

REPO_SLUG = "acme/widget"
TARGET_ROOT = Path("/tmp/orchestrator-test-target-root")
REFRESH_BASE = "_refresh_base_and_worktrees"
FANOUT_START_TIMEOUT_MESSAGE = "implementing fanout #1 did not start"
POLL_INTERVAL_SECONDS = 0.01
EVENT_TIMEOUT_SECONDS = 2.0
WORKER_TIMEOUT_SECONDS = 5.0
DEFERRED_ISSUE_NUMBERS = (10, 11, 12)
FAMILY_ISSUE_NUMBER = 42
RELABELLED_FANOUT_ISSUE_NUMBER = 50


class BacklogDispatchFilterTest(_BacklogDispatchFixture):
    def test_backlog_only_does_not_starve_fanout(self) -> None:
        self._assert_parked_does_not_starve_fanout(BACKLOG_LABEL)

    def test_paused_only_does_not_starve_fanout(self) -> None:
        self._assert_parked_does_not_starve_fanout(PAUSED_LABEL)

    def test_backlog_blocked_bucket_stays_exempt(self) -> None:
        # A `blocked` parent and a parked `backlog` issue share the family
        # bucket. The backlog issue carries no workflow label, so leaving it
        # in would force `cap_exempt=False`, reserve the only slot, and
        # starve the `implementing` fanout. Filtered at dispatch, the bucket
        # is `blocked`-only -> cap-exempt, so BOTH the blocked parent and
        # the fanout implementer run this tick.
        sched = self._scheduler(per_repo_cap=1)
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=LABEL_IMPLEMENTING))
        gh.add_issue(make_issue(2, label=LABEL_BLOCKED))
        gh.add_issue(self._parked_issue(3, BACKLOG_LABEL))

        process = self._processor(1, 2)
        with (
            patch_base_refresh(),
            _patch_process_issue(
                side_effect=process,
            ),
        ):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(
                process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS),
                FANOUT_START_TIMEOUT_MESSAGE,
            )
            self.assertTrue(
                process.starts[2].wait(timeout=EVENT_TIMEOUT_SECONDS),
                "blocked #2 did not start -- the bucket must stay cap-exempt once the backlog issue is filtered out",
            )
        process.release_all()
        self._wait_idle(sched)
        self.assertNotIn(
            3,
            process.processed_snapshot(),
            "backlog #3 must be filtered at dispatch, never processed",
        )
