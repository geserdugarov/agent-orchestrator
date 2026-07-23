# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Merged, closed, and manually stopped terminal-drain tests."""
from __future__ import annotations

import unittest

from tests import workflow_drain_test_support as support


class DrainReviewPrTerminalTest(unittest.TestCase, support._DrainTestMixin):
    """Merged, closed, and manually stopped PRs take distinct exits."""

    def test_merged_pr_finalizes_to_done(self) -> None:
        # The merged arc: stamp `merged_at`, flip to `done`, emit
        # `pr_merged` with `merge_method="external"` and the supplied
        # stage, close the issue if still open, and run branch cleanup.
        scenario = support._DrainScenario(
            support._MERGED_PR_ISSUE_NUMBER,
            support._MERGED_PR_NUMBER,
            True,
            support.STATE_CLOSED,
            support.LABEL_FIXING,
        )
        context = support._seed_terminal(
            scenario,
            review_round=2,
            conflict_round=0,
            branch=support._issue_branch(support._MERGED_PR_ISSUE_NUMBER),
        )
        drain_result = self._drain(context)
        self._assert_cleanup(
            drain_result,
            label=support.LABEL_DONE,
            state_key="merged_at",
        )
        event = self._only_event(drain_result, support.EVENT_PR_MERGED)
        self.assertEqual(
            (
                event[support._STAGE_KEY],
                event["pr_number"],
                event["merge_method"],
                event["sha"],
                event["review_round"],
            ),
            (
                support.LABEL_FIXING,
                support._MERGED_PR_NUMBER,
                support.MERGE_METHOD_EXTERNAL,
                support.DEFAULT_HEAD_SHA,
                2,
            ),
        )

    def test_closed_unmerged_pr_finalizes_to_rejected(
        self,
    ) -> None:
        # The closed-PR arc: stamp `closed_without_merge_at`, flip to
        # `rejected`, emit `pr_closed_without_merge` with the supplied
        # stage, close the issue if still open, and run branch cleanup.
        # The branch is dead weight once the PR is gone, mirroring the
        # merged-PR cleanup order.
        scenario = support._DrainScenario(
            support._CLOSED_PR_ISSUE_NUMBER,
            support._CLOSED_PR_NUMBER,
            False,
            support.STATE_CLOSED,
            support.LABEL_RESOLVING_CONFLICT,
            sha="dead0001",
        )
        context = support._seed_terminal(
            scenario,
            review_round=3,
            conflict_round=2,
            branch=support._issue_branch(support._CLOSED_PR_ISSUE_NUMBER),
        )
        drain_result = self._drain(context)
        self._assert_cleanup(
            drain_result,
            label=support.LABEL_REJECTED,
            state_key="closed_without_merge_at",
        )
        event = self._only_event(
            drain_result,
            support.EVENT_PR_CLOSED_WITHOUT_MERGE,
        )
        self.assertEqual(
            (
                event[support._STAGE_KEY],
                event["pr_number"],
                event["sha"],
                event["review_round"],
                event[support._CONFLICT_ROUND_KEY],
            ),
            (
                support.LABEL_RESOLVING_CONFLICT,
                support._CLOSED_PR_NUMBER,
                "dead0001",
                3,
                2,
            ),
        )

    def test_open_pr_closed_issue_rejects_no_cleanup(
        self,
    ) -> None:
        # Open PR + manually closed issue is a human stop signal: flip
        # to `rejected` so the in_review HITL ready-ping cannot
        # advertise the PR as ready for human merge over the human
        # rejection, but deliberately leave the branch alone so the
        # operator can salvage / reopen the still-open PR. No event
        # emit either -- `pr_closed_without_merge` is reserved for the
        # genuine closed-PR arc above.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._MANUALLY_CLOSED_ISSUE_NUMBER,
            label=support.LABEL_IN_REVIEW,
        )
        issue.closed = True
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._MANUALLY_CLOSED_PR_NUMBER,
            head_branch=support._issue_branch(support._MANUALLY_CLOSED_ISSUE_NUMBER),
            head=support.FakePRRef(sha=support.DEFAULT_HEAD_SHA),
            merged=False, state=support.STATE_OPEN,
        )
        gh.add_pr(pr)
        state = support._state_with_pr_number(
            gh,
            support._MANUALLY_CLOSED_ISSUE_NUMBER,
            support._MANUALLY_CLOSED_PR_NUMBER,
        )

        mocks = self._run(
            lambda: self.assertTrue(
                support.workflow._drain_review_pr_terminals(
                    gh, support._TEST_SPEC, issue, state, pr, stage=support.LABEL_IN_REVIEW,
                )
            ),
            run_agent=support._agent(),
        )

        self.assertIn(
            (support._MANUALLY_CLOSED_ISSUE_NUMBER, support.LABEL_REJECTED),
            gh.label_history,
        )
        self.assertIn("closed_without_merge_at", state.data)
        # The PR is still open and may be reopened / salvaged, so the
        # branch must survive this exit.
        mocks[support._CLEANUP_MOCK_KEY].assert_not_called()
        # No `pr_closed_without_merge` emit for the open-PR case.
        self.assertEqual(
            [event for event in gh.recorded_events
             if event[support._EVENT_KEY] == support.EVENT_PR_CLOSED_WITHOUT_MERGE],
            [],
        )
        self.assertEqual(
            [
                event for event in gh.recorded_events
                if event[support._EVENT_KEY] == support.EVENT_PR_MERGED
            ],
            [],
        )
