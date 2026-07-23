# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import workflow

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    LABEL_IN_REVIEW,
    LABEL_VALIDATING,
    STATE_CLOSED,
    STATE_OPEN,
)

from tests.scheduler_routing_fakes import (
    _PyGithubIssue,
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
                "open validating #1 did not start",
            )
            self.assertTrue(
                process.starts[2].wait(timeout=EVENT_TIMEOUT_SECONDS),
                "closed in_review #2 was starved by the per-repo cap -- a terminal finalization must run cap-exempt",
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
        with (
            patch.object(workflow, REFRESH_BASE),
            patch.object(
                workflow,
                PROCESS_ISSUE,
                side_effect=process,
            ),
        ):
            workflow.tick(gh, self._spec(parallel_limit=4), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS))
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
        with (
            patch.object(workflow, REFRESH_BASE),
            patch.object(
                workflow,
                PROCESS_ISSUE,
                side_effect=process,
            ),
        ):
            workflow.tick(gh, self._spec(parallel_limit=1), scheduler=sched)
            self.assertTrue(process.starts[1].wait(timeout=EVENT_TIMEOUT_SECONDS))
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
