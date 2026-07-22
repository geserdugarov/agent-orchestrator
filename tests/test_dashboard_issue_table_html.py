# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard issue-table HTML tests."""

import unittest


from dataclasses import dataclass


from datetime import datetime, timezone


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_MATCH_STANDALONE_MOCK_ROW_AR = 12.0


_PILL_WITHOUT_FAILURES_ROW_AR = 4.0


_FAIL_PILL_FAILURES_ROW_ARGUM = 4.0


_BAR_RELATIVE_MAX_ROW_ARGUMEN = 10.0


_BAR_RELATIVE_MAX_ROW_SECONDARY = 5.0


_MORE_WARN_TONE_ROW_ARGUMENT = 4.0


_YEAR = 2026


FIRST_SEEN = datetime(_YEAR, 5, 1, tzinfo=timezone.utc)


LAST_SEEN = datetime(_YEAR, 5, 2, tzinfo=timezone.utc)


REPO_A = "acme/a"


REPO_B = "acme/b"


STAGE_IMPLEMENTING = "implementing"


@dataclass(frozen=True)
class _IssueRowCase:
    repo: str
    issue: int
    cost: float | None
    failed: int = 0
    max_round: int | None = None
    max_retry: int | None = None


COLUMN_RUNS = "Runs"


class IssuesTableHtmlTest(unittest.TestCase):
    """The "Most expensive issues" panel is hand-rolled HTML (rather
    than `st.dataframe`) so it can carry the standalone mock's
    in-row cost bars and clean / fail status pills.
    """

    def test_columns_match_standalone_mock(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, _MATCH_STANDALONE_MOCK_ROW_AR)]
        html = dashboard._issues_table_html(rows)
        for header in ("Issue", "Cost", COLUMN_RUNS, "Review rds", "Retries", "Status"):
            self.assertIn(f">{header}<", html)

    def test_clean_pill_without_failures(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, _PILL_WITHOUT_FAILURES_ROW_AR, failed=0)]
        html = dashboard._issues_table_html(rows)
        self.assertIn('class="orch-pill ok"', html)
        self.assertIn(">clean<", html)
        self.assertNotIn('class="orch-pill bad"', html)

    def test_fail_pill_with_failures(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, _FAIL_PILL_FAILURES_ROW_ARGUM, failed=3)]
        html = dashboard._issues_table_html(rows)
        self.assertIn('class="orch-pill bad"', html)
        self.assertIn(">3 fail<", html)

    def test_in_row_cost_bar_relative_to_max(self) -> None:
        # Cheapest issue's bar is a fraction of the most expensive
        # issue's full-width bar.
        _, dashboard = _reload()
        rows = [
            self._row(REPO_A, 1, _BAR_RELATIVE_MAX_ROW_ARGUMEN),
            self._row(REPO_B, 2, _BAR_RELATIVE_MAX_ROW_SECONDARY),
        ]
        html = dashboard._issues_table_html(rows)
        # Full-width bar on the most expensive issue and a half-
        # width bar on the cheaper one.
        self.assertIn("width:100.0%", html)
        self.assertIn("width:50.0%", html)

    def test_review_rounds_three_or_more_warn_tone(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, _MORE_WARN_TONE_ROW_ARGUMENT, max_round=4)]
        html = dashboard._issues_table_html(rows)
        # High-review-round cells get the warn class so the operator
        # can spot rework-heavy issues at a glance.
        self.assertIn('class="orch-badge-warn">4', html)

    def _row(self, *row_fields, **options):
        _, dashboard = _reload()
        from orchestrator.analytics.read import IssueSummaryRow

        case = _IssueRowCase(*row_fields, **options)
        return IssueSummaryRow(
            repo=case.repo,
            issue=case.issue,
            event_count=10,
            first_seen=FIRST_SEEN,
            last_seen=LAST_SEEN,
            latest_stage=STAGE_IMPLEMENTING,
            agent_exits=4,
            total_cost_usd=case.cost,
            total_input_tokens=0,
            total_output_tokens=0,
            max_review_round=case.max_round,
            failed_agent_runs=case.failed,
            max_retry_count=case.max_retry,
        )
