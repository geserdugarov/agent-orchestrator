# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from orchestrator import workflow
from orchestrator.github import BACKLOG_LABEL, PAUSED_LABEL

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    LABEL_BLOCKED,
    LABEL_IMPLEMENTING,
)

from tests.scheduler_routing_test_support import (
    _BacklogDispatchFixture,
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
        with (
            patch.object(workflow, REFRESH_BASE),
            patch.object(
                workflow,
                PROCESS_ISSUE,
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
