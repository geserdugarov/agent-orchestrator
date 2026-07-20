# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

# The source each guarded reader must keep scanning; the regression
# guard asserts the reader stays put and never silently moves to the
# day-bucketed rollup.
_BASE_TABLE = "FROM analytics_events"
_ROLLUP_TABLE = "FROM analytics_daily_rollup"
_AGENT_RUNS_TABLE = "FROM analytics_agent_runs"


class RawReaderRollupKeepsTest(unittest.TestCase):
    """The issue is explicit about which readers stay on the raw
    table or the agent-run view: recent agent exits, top-cost
    issues, review-round breakdown, hourly heatmap, issue events,
    and cost coverage. The other view-backed read
    (`get_backend_daily_tokens`) and `get_event_breakdown` also stay
    where they are. This test class is a regression guard so a
    future change cannot quietly move one of them to the rollup
    where it would lose row-level detail (`ts` precision,
    `review_round`, `retry_count`, hour-of-day).
    """

    def test_recent_agent_exits_reads_base_table(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_recent_agent_exits(connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn(_BASE_TABLE, sql)
        self.assertNotIn(_ROLLUP_TABLE, sql)

    def test_top_cost_issues_reads_base_table(self) -> None:
        # `get_issues` carries MIN(ts), MAX(ts), `latest_stage`,
        # MAX(review_round), and MAX(retry_count) which the rollup
        # cannot answer -- the rollup throws away the per-row `ts`
        # precision and never carried `review_round` / `retry_count`.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_issues(connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn(_BASE_TABLE, sql)
        self.assertNotIn(_ROLLUP_TABLE, sql)

    def test_review_round_breakdown_stays_on_view(self) -> None:
        # `review_round` is not in the rollup key, so the rollup
        # cannot bucket by it. Stays on `analytics_agent_runs`.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_review_round_breakdown(connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn(_AGENT_RUNS_TABLE, sql)
        self.assertNotIn(_ROLLUP_TABLE, sql)

    def test_hourly_heatmap_stays_on_base_table(self) -> None:
        # The rollup is day-bucketed -- hour-of-day is not
        # recoverable from `day`, so this widget must keep reading
        # from `analytics_events`.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_hourly_heatmap(connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn(_BASE_TABLE, sql)
        self.assertNotIn(_ROLLUP_TABLE, sql)

    def test_issue_events_stays_on_base_table(self) -> None:
        # Per-row drill-down -- the rollup pre-aggregates per group
        # so individual rows are no longer addressable.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_issue_events(
            repo="owner/r", issue=1, connect=conn.as_connect,
        )
        sql, _ = conn.first_query
        self.assertIn(_BASE_TABLE, sql)
        self.assertNotIn(_ROLLUP_TABLE, sql)

    def test_cost_coverage_stays_on_view(self) -> None:
        # Cost coverage stays on `analytics_agent_runs` per the
        # issue's "unless the rollup can match behavior exactly"
        # guardrail -- being conservative here lets the
        # `unknown-price` cohort's run / token accounting stay
        # exactly as it was.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_cost_coverage(connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn(_AGENT_RUNS_TABLE, sql)
        self.assertNotIn(_ROLLUP_TABLE, sql)
