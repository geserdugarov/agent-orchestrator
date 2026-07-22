# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard expensive-issue ranking tests."""

import unittest


from datetime import datetime, timezone


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_YEAR = 2026


FIRST_SEEN = datetime(_YEAR, 5, 1, tzinfo=timezone.utc)


LAST_SEEN = datetime(_YEAR, 5, 2, tzinfo=timezone.utc)


REPO_A = "acme/a"


REPO_B = "acme/b"


REPO_C = "acme/c"


STAGE_IMPLEMENTING = "implementing"


class TopExpensiveIssuesTest(unittest.TestCase):
    def test_sorts_by_cost_desc(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue(REPO_A, 1, 0.1),
            self._issue(REPO_B, 2, 1.0),
            self._issue(REPO_C, 3, 0.5),
        ]
        top = dashboard.top_expensive_issues(rows, limit=2)
        self.assertEqual(
            [(row.repo, row.issue) for row in top],
            [(REPO_B, 2), (REPO_C, 3)],
        )

    def test_none_cost_sorts_last(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue(REPO_A, 1, None),
            self._issue(REPO_B, 2, 0.1),
        ]
        top = dashboard.top_expensive_issues(rows, limit=5)
        self.assertEqual([row.issue for row in top], [2, 1])

    def test_limit_zero_returns_empty(self) -> None:
        _, dashboard = _reload()
        rows = [self._issue(REPO_A, 1, 0.1)]
        self.assertEqual(dashboard.top_expensive_issues(rows, limit=0), [])

    def test_ties_break_on_event_count_then_identity(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue(REPO_A, 1, 1.0, events=2),
            self._issue(REPO_A, 2, 1.0, events=10),
            self._issue(REPO_B, 1, 1.0, events=2),
        ]
        top = dashboard.top_expensive_issues(rows)
        # Higher event count first, then (repo, issue) ascending.
        self.assertEqual(
            [(row.repo, row.issue) for row in top],
            [(REPO_A, 2), (REPO_A, 1), (REPO_B, 1)],
        )

    def _issue(self, repo, num, cost, events=1):
        _, dashboard = _reload()
        from orchestrator.analytics.read import IssueSummaryRow

        return IssueSummaryRow(
            repo=repo,
            issue=num,
            event_count=events,
            first_seen=FIRST_SEEN,
            last_seen=LAST_SEEN,
            latest_stage=STAGE_IMPLEMENTING,
            agent_exits=1,
            total_cost_usd=cost,
            total_input_tokens=0,
            total_output_tokens=0,
        )
