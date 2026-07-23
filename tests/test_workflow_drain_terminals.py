# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""No-op and open-state terminal-drain tests."""
from __future__ import annotations

import unittest

from tests import workflow_drain_test_support as support


class DrainReviewPrTerminalsTest(unittest.TestCase, support._PatchedWorkflowMixin):
    """Direct coverage of the shared `_drain_review_pr_terminals` helper.

    `_handle_in_review`, `_handle_fixing`, and `_handle_resolving_conflict`
    all delegate their terminal arcs (merged PR -> `done`, closed PR ->
    `rejected`, open PR + manually-closed issue -> `rejected` without
    branch cleanup) to this helper. The per-stage handler tests cover the
    integrated behavior; these focused tests pin the helper contract
    (return value, event shape, branch-cleanup semantics, pr=None no-op)
    independently of any stage wiring.
    """

    def test_pr_none_returns_false_no_op(self) -> None:
        # Fixing's PR-fetch failure path sets `pr=None` and hands it
        # straight to the helper; the helper must treat that as a no-op
        # so the calling handler can fall through to its own fetch-
        # failure deferral (the `if pr is None: return` guard further
        # down the fixing body). No label change, no state writes, no
        # cleanup, no events.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(support._NO_PR_ISSUE_NUMBER, label=support.LABEL_FIXING)
        gh.add_issue(issue)
        state = support._state_with_pr_number(
            gh,
            support._NO_PR_ISSUE_NUMBER,
            support._NO_PR_NUMBER,
        )

        mocks = self._run(
            lambda: self.assertFalse(
                support.workflow._drain_review_pr_terminals(
                    gh, support._TEST_SPEC, issue, state, None, stage=support.LABEL_FIXING,
                )
            ),
            run_agent=support._agent(),
        )

        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        mocks[support._CLEANUP_MOCK_KEY].assert_not_called()
        self.assertEqual(gh.recorded_events, [])

    def test_open_pr_open_issue_returns_false(self) -> None:
        # The handler-side rescan / debounce / drift logic depends on
        # the helper returning False for a "nothing terminal" state so
        # the caller can continue with the same `pr`.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(support._OPEN_PR_ISSUE_NUMBER, label=support.LABEL_IN_REVIEW)
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._OPEN_PR_NUMBER,
            head_branch=support._issue_branch(support._OPEN_PR_ISSUE_NUMBER),
            head=support.FakePRRef(sha=support.DEFAULT_HEAD_SHA),
            merged=False, state=support.STATE_OPEN,
        )
        gh.add_pr(pr)
        state = support._state_with_pr_number(
            gh,
            support._OPEN_PR_ISSUE_NUMBER,
            support._OPEN_PR_NUMBER,
        )

        mocks = self._run(
            lambda: self.assertFalse(
                support.workflow._drain_review_pr_terminals(
                    gh, support._TEST_SPEC, issue, state, pr, stage=support.LABEL_IN_REVIEW,
                )
            ),
            run_agent=support._agent(),
        )

        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        mocks[support._CLEANUP_MOCK_KEY].assert_not_called()
        self.assertEqual(gh.recorded_events, [])
