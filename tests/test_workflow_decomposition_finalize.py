# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import (
    FakeGitHubClient,
    FakeIssue,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class ChildMergedPrAutoFinalizeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A child whose linked PR was merged externally but whose workflow
    label was never advanced past an in-flight stage (e.g. `validating`)
    used to look like a `manually_closed` child to `_handle_blocked` /
    `_handle_umbrella` and park the parent for human adjudication. The
    finalize helper detects the merge during the parent's poll and flips
    the child to `done`, so the parent's aggregation can proceed.
    """

    def _seed_child_with_merged_pr(
        self, gh: FakeGitHubClient, *, number: int, label: str, pr_number: int,
    ) -> FakeIssue:
        child = make_issue(number, label=label)
        child.closed = True
        gh.add_issue(child)
        pr = FakePR(
            number=pr_number,
            head_branch=f"orchestrator/geserdugarov__agent-orchestrator/issue-{number}",
            head=FakePRRef(sha="cafe1234"),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(number, pr_number=pr_number)
        return child

    def test_blocked_recovers_child_with_merged_pr(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(70, label="blocked")
        gh.add_issue(parent)
        done_child = make_issue(701, label="done")
        done_child.closed = True
        gh.add_issue(done_child)
        # children[1]: a `validating` child whose PR was merged externally
        # (the human clicked Merge before the reviewer agent finished).
        # Used to park the parent on "manually closed"; must now be
        # finalized in-line and counted toward the all-done aggregation.
        self._seed_child_with_merged_pr(
            gh, number=702, label="validating", pr_number=7020,
        )
        gh.seed_state(70, children=[701, 702])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn((702, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(702))
        # Parent flipped to ready because every child is now `done`.
        self.assertIn((70, "ready"), gh.label_history)
        # No manual-close park comment posted.
        self.assertFalse(any(
            "closed without reaching" in body
            for n, body in gh.posted_comments if n == 70
        ))

    def test_umbrella_recovers_child_with_merged_pr(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(80, label="umbrella")
        gh.add_issue(parent)
        done_child = make_issue(801, label="done")
        done_child.closed = True
        gh.add_issue(done_child)
        self._seed_child_with_merged_pr(
            gh, number=802, label="implementing", pr_number=8020,
        )
        gh.seed_state(80, children=[801, 802], umbrella=True)

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn((802, "done"), gh.label_history)
        # Umbrella closes once both children are `done`.
        self.assertIn((80, "done"), gh.label_history)
        self.assertTrue(parent.closed)
        self.assertFalse(any(
            "closed without reaching" in body
            for n, body in gh.posted_comments if n == 80
        ))

    def test_blocked_still_parks_when_child_pr_not_merged(self) -> None:
        # Regression guard: when the child PR is closed-without-merge,
        # the finalize helper must NOT flip the child to `done`. The
        # original manually-closed park still fires.
        gh = FakeGitHubClient()
        parent = make_issue(71, label="blocked")
        gh.add_issue(parent)
        closed_child = make_issue(711, label="validating")
        closed_child.closed = True
        gh.add_issue(closed_child)
        pr = FakePR(
            number=7110,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-711",
            head=FakePRRef(sha="cafe1234"),
            merged=False,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(711, pr_number=7110)
        gh.seed_state(71, children=[711])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertNotIn((711, "done"), gh.label_history)
        self.assertTrue(gh.pinned_data(71).get("awaiting_human"))
