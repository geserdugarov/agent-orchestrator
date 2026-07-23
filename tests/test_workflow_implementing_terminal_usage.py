# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing terminal usage behavior."""

from __future__ import annotations

import unittest

from tests import implementing_terminal_test_support as support

FakeGitHubClient = support.FakeGitHubClient
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
LABEL_REJECTED = support.LABEL_REJECTED
NO_USAGE_ISSUE = support.NO_USAGE_ISSUE
USAGE_COST_USD = support.USAGE_COST_USD
USAGE_ISSUE = support.USAGE_ISSUE
USAGE_TOKEN_COUNT = support.USAGE_TOKEN_COUNT
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
make_issue = support.make_issue
workflow = support.workflow


class FinalizeIfIssueClosedUsageVerdictTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The closed-issue counterpart to `_finalize_if_pr_merged` posts the
    terminal usage receipt as a tracked comment BEFORE its single
    `write_pinned_state`, and skips it when no run was ever counted.
    Exercised directly so the receipt is pinned independently of any
    caller (the closed-issue sweep seeds no counters, so an integration
    test alone would stay green if the receipt call were removed)."""

    def test_rejected_posts_usage_verdict(self) -> None:
        from orchestrator.github import PinnedState

        gh = FakeGitHubClient()
        issue = make_issue(USAGE_ISSUE, label=LABEL_IMPLEMENTING)
        issue.closed = True
        gh.add_issue(issue)
        # No linked PR: the closed issue flips straight to `rejected`; the
        # receipt still surfaces the cumulative verdict, tracked before the
        # single write.
        seed = dict(
            issue_agent_runs=2,
            issue_total_tokens=USAGE_TOKEN_COUNT,
            issue_total_cost_usd=USAGE_COST_USD,
            issue_cost_sources=["reported"],
        )
        gh.seed_state(USAGE_ISSUE, **seed)
        self._state = PinnedState(comment_id=None, data=dict(seed))

        self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_issue_closed(
                    gh,
                    _TEST_SPEC,
                    issue,
                    self._state,
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((USAGE_ISSUE, LABEL_REJECTED), gh.label_history)
        self._receipts = [
            body
            for issue_number, body in gh.posted_comments
            if issue_number == USAGE_ISSUE and body.startswith(":receipt:")
        ]
        self.assertEqual(len(self._receipts), 1)
        self.assertIn(
            "this issue: 2 agent runs · 3,400 tokens · $0.31",
            self._receipts[0],
        )
        self._receipt_comments = [comment for comment in issue.comments if comment.body.startswith(":receipt:")]
        self._receipt_comment = self._receipt_comments[0]
        self.assertIn(
            self._receipt_comment.id,
            gh.pinned_data(USAGE_ISSUE).get("orchestrator_comment_ids", []),
        )

    def test_no_counters_posts_no_verdict(self) -> None:
        from orchestrator.github import PinnedState

        gh = FakeGitHubClient()
        issue = make_issue(NO_USAGE_ISSUE, label=LABEL_IMPLEMENTING)
        issue.closed = True
        gh.add_issue(issue)
        gh.seed_state(NO_USAGE_ISSUE)

        self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_issue_closed(
                    gh,
                    _TEST_SPEC,
                    issue,
                    PinnedState(comment_id=None, data={}),
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((NO_USAGE_ISSUE, LABEL_REJECTED), gh.label_history)
        self.assertEqual(
            [
                body
                for issue_number, body in gh.posted_comments
                if issue_number == NO_USAGE_ISSUE and body.startswith(":receipt:")
            ],
            [],
        )
