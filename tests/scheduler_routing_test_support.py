# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import contextlib
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import config, workflow
from orchestrator.github import BACKLOG_LABEL
from orchestrator.scheduler import IssueScheduler

from tests.fakes import FakeGitHubClient, FakeLabel, make_issue
from tests.workflow_helpers import (
    LABEL_IMPLEMENTING,
    TEST_BASE_BRANCH,
)

from tests.scheduler_routing_workers import (
    _IssueProcessor,
    _SequentialIssueProcessor,
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
        deadline_s: float = WORKER_TIMEOUT_SECONDS,
    ) -> None:
        deadline = time.monotonic() + deadline_s
        while scheduler.active_count(repo_slug) > 0 and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
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
        timeout: float = EVENT_TIMEOUT_SECONDS,
    ) -> None:
        deadline = time.monotonic() + timeout
        while scheduler.is_active(REPO_SLUG, issue_number) and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
        self.assertFalse(scheduler.is_active(REPO_SLUG, issue_number))

    @contextlib.contextmanager
    def _route_through(self, processor):
        with (
            patch.object(workflow, REFRESH_BASE),
            patch.object(
                workflow,
                PROCESS_ISSUE,
                side_effect=processor,
            ),
        ):
            yield


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
        with self._route_through(process):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(
                process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS),
                f"implementing #1 was starved -- the {parked_label} issue must not occupy the only per-repo slot",
            )
        process.release_all()
        self._wait_idle(sched)
        self.assertNotIn(
            2,
            process.processed_snapshot(),
            f"{parked_label} #2 must be filtered at dispatch, never processed",
        )
