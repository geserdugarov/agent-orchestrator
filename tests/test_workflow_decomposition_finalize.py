# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

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

LABEL_DONE = "done"
BLOCKED_PARENT_NUMBER = 70
BLOCKED_DONE_CHILD_NUMBER = 701
BLOCKED_MERGED_CHILD_NUMBER = 702
BLOCKED_MERGED_PR_NUMBER = 7020
UMBRELLA_PARENT_NUMBER = 80
UMBRELLA_DONE_CHILD_NUMBER = 801
UMBRELLA_MERGED_CHILD_NUMBER = 802
UMBRELLA_MERGED_PR_NUMBER = 8020
UNMERGED_PARENT_NUMBER = 71
UNMERGED_CHILD_NUMBER = 711
UNMERGED_PR_NUMBER = 7110


def _seed_child_with_merged_pr(
    gh: FakeGitHubClient,
    *,
    number: int,
    label: str,
    pr_number: int,
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


class ChildMergedPrAutoFinalizeTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A child whose linked PR was merged externally but whose workflow
    label was never advanced past an in-flight stage (e.g. `validating`)
    looks like a manually closed child to the parent aggregation. The
    finalize helper detects the merge during the parent's poll and flips
    the child to `done`, so the parent's aggregation can proceed.
    """

    def test_blocked_recovers_child_with_merged_pr(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(BLOCKED_PARENT_NUMBER, label="blocked")
        gh.add_issue(parent)
        done_child = make_issue(BLOCKED_DONE_CHILD_NUMBER, label=LABEL_DONE)
        done_child.closed = True
        gh.add_issue(done_child)
        # children[1]: a `validating` child whose PR was merged externally
        # (the human clicked Merge before the reviewer agent finished).
        # Used to park the parent on "manually closed"; must now be
        # finalized in-line and counted toward the all-done aggregation.
        _seed_child_with_merged_pr(
            gh,
            number=BLOCKED_MERGED_CHILD_NUMBER,
            label="validating",
            pr_number=BLOCKED_MERGED_PR_NUMBER,
        )
        gh.seed_state(
            BLOCKED_PARENT_NUMBER,
            children=[BLOCKED_DONE_CHILD_NUMBER, BLOCKED_MERGED_CHILD_NUMBER],
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn(
            (BLOCKED_MERGED_CHILD_NUMBER, LABEL_DONE),
            gh.label_history,
        )
        self.assertIn("merged_at", gh.pinned_data(BLOCKED_MERGED_CHILD_NUMBER))
        # Parent flipped to ready because every child is now `done`.
        self.assertIn((BLOCKED_PARENT_NUMBER, "ready"), gh.label_history)
        # No manual-close park comment posted.
        self.assertFalse(
            any(
                "closed without reaching" in body
                for issue_number, body in gh.posted_comments
                if issue_number == BLOCKED_PARENT_NUMBER
            )
        )

    def test_umbrella_recovers_child_with_merged_pr(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(UMBRELLA_PARENT_NUMBER, label="umbrella")
        gh.add_issue(parent)
        done_child = make_issue(UMBRELLA_DONE_CHILD_NUMBER, label=LABEL_DONE)
        done_child.closed = True
        gh.add_issue(done_child)
        _seed_child_with_merged_pr(
            gh,
            number=UMBRELLA_MERGED_CHILD_NUMBER,
            label="implementing",
            pr_number=UMBRELLA_MERGED_PR_NUMBER,
        )
        gh.seed_state(
            UMBRELLA_PARENT_NUMBER,
            children=[UMBRELLA_DONE_CHILD_NUMBER, UMBRELLA_MERGED_CHILD_NUMBER],
            umbrella=True,
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn(
            (UMBRELLA_MERGED_CHILD_NUMBER, LABEL_DONE),
            gh.label_history,
        )
        # Umbrella closes once both children are `done`.
        self.assertIn((UMBRELLA_PARENT_NUMBER, LABEL_DONE), gh.label_history)
        self.assertTrue(parent.closed)
        self.assertFalse(
            any(
                "closed without reaching" in body
                for issue_number, body in gh.posted_comments
                if issue_number == UMBRELLA_PARENT_NUMBER
            )
        )

    def test_unmerged_child_pr_keeps_parent_parked(self) -> None:
        # Regression guard: when the child PR is closed-without-merge,
        # the finalize helper must NOT flip the child to `done`. The
        # original manually-closed park still fires.
        gh = FakeGitHubClient()
        parent = make_issue(UNMERGED_PARENT_NUMBER, label="blocked")
        gh.add_issue(parent)
        closed_child = make_issue(UNMERGED_CHILD_NUMBER, label="validating")
        closed_child.closed = True
        gh.add_issue(closed_child)
        pr = FakePR(
            number=UNMERGED_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-711",
            head=FakePRRef(sha="cafe1234"),
            merged=False,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(UNMERGED_CHILD_NUMBER, pr_number=UNMERGED_PR_NUMBER)
        gh.seed_state(UNMERGED_PARENT_NUMBER, children=[UNMERGED_CHILD_NUMBER])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertNotIn(
            (UNMERGED_CHILD_NUMBER, LABEL_DONE),
            gh.label_history,
        )
        self.assertTrue(gh.pinned_data(UNMERGED_PARENT_NUMBER).get("awaiting_human"))
