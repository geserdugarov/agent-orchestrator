# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Terminal-drain receipt and closed-issue tests."""
from __future__ import annotations

import unittest

from tests import workflow_drain_test_support as support


class DrainReviewPrReceiptTest(unittest.TestCase, support._DrainTestMixin):
    """Terminal drains tolerate closed issues and persist usage receipts."""

    def test_merged_arc_handles_already_closed_issue(
        self,
    ) -> None:
        # A `Resolves #N` footer auto-closes the issue the moment the PR
        # merges, so when the closed-issue sweep yields this case the
        # helper sees an already-closed issue. The merged arc still
        # finalizes the label, but must not crash trying to re-close
        # what GitHub already closed.
        scenario = support._DrainScenario(
            support._ALREADY_CLOSED_ISSUE_NUMBER,
            support._ALREADY_CLOSED_PR_NUMBER,
            True,
            support.STATE_CLOSED,
            support.LABEL_FIXING,
            issue_closed=True,
            sha="feed0001",
        )
        drain_result = self._drain(support._seed_terminal(scenario))
        event = self._only_event(drain_result, support.EVENT_PR_MERGED)
        self.assertIn(
            (support._ALREADY_CLOSED_ISSUE_NUMBER, support.LABEL_DONE),
            drain_result.context.gh.label_history,
        )
        self.assertTrue(drain_result.context.issue.closed)
        self.assertEqual(event[support._STAGE_KEY], support.LABEL_FIXING)

    def test_each_terminal_posts_usage_verdict(self) -> None:
        # All three terminal arcs -- merged -> done, closed -> rejected, and
        # the open-PR + manually-closed-issue -> rejected path -- surface the
        # cumulative usage verdict as a tracked comment posted BEFORE the
        # arc's `write_pinned_state`, so its id rides the persisted state.
        for scenario in support.RECEIPT_SCENARIOS:
            with self.subTest(stage=scenario.stage):
                self._assert_usage_receipt(scenario)
