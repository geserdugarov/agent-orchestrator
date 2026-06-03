# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Plotly figure builders in `orchestrator.dashboard_charts`.

Plotly lives in the optional `dashboard` dependency group, so the
default `uv sync --locked` does not install it. These tests
skip cleanly when the module is unavailable -- the import guard at
the top of the file prevents pytest collection from failing on a
fresh `uv sync --locked` checkout, and `@unittest.skipUnless(...)`
labels every case so the skip reason is visible in CI output.

The chart module imports plotly at module load (it is reachable only
from the lazy `import dashboard_charts` inside `dashboard.main`), so
under the default sync `import orchestrator.dashboard_charts` raises
`ModuleNotFoundError`. We catch that same exception class instead of
checking `plotly` by name, so a future move to `kaleido` / a Plotly
extras pin does not silently make the suite skip too eagerly.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

try:
    from orchestrator import dashboard_charts
    from orchestrator.analytics.read import (
        AgentExitRow,
        IssueSummaryRow,
        StageBreakdown,
        TimeSeriesPoint,
    )
    HAS_PLOTLY = True
except ModuleNotFoundError:
    HAS_PLOTLY = False
    dashboard_charts = None  # type: ignore[assignment]


_SKIP_REASON = "plotly not installed -- run `uv sync --group dashboard`"


def _agent_exit(
    *,
    review_round=None,
    cost_source="reported",
) -> "AgentExitRow":
    return AgentExitRow(
        ts=datetime(2026, 5, 1, 12, 0),
        repo="acme/widgets",
        issue=1,
        stage="implementing",
        agent_role="dev",
        backend="claude",
        duration_s=12.5,
        exit_code=0,
        timed_out=False,
        review_round=review_round,
        retry_count=0,
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.04,
        cost_source=cost_source,
    )


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class UsageOverTimeTest(unittest.TestCase):

    def test_pivots_event_series(self) -> None:
        # Two events on day A, one on day B -- expect two traces
        # (one per event), each with two y-values aligned to the
        # sorted-day x-axis. Days with no event for a series fall
        # through to 0.
        points = [
            TimeSeriesPoint(day=date(2026, 5, 1), event="agent_exit", count=2),
            TimeSeriesPoint(day=date(2026, 5, 1), event="stage_enter", count=5),
            TimeSeriesPoint(day=date(2026, 5, 2), event="agent_exit", count=3),
        ]
        fig = dashboard_charts.usage_over_time(points)
        traces = list(fig.data)
        self.assertEqual(len(traces), 2)
        names = {t.name for t in traces}
        self.assertEqual(names, {"agent_exit", "stage_enter"})
        # Stacked layout is what the dashboard expects.
        self.assertEqual(fig.layout.barmode, "stack")

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.usage_over_time([])
        self.assertEqual(len(fig.data), 0)
        # Placeholder annotation present so the empty state reads
        # as "no data" rather than "broken chart".
        self.assertGreaterEqual(len(fig.layout.annotations), 1)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class StageBarsTest(unittest.TestCase):

    def test_renders_one_bar_per_stage(self) -> None:
        rows = [
            StageBreakdown(stage="implementing", count=10),
            StageBreakdown(stage="validating", count=4),
        ]
        fig = dashboard_charts.stage_bars(rows)
        self.assertEqual(len(fig.data), 1)
        bar = fig.data[0]
        # Horizontal bars (long stage names need the room).
        self.assertEqual(bar.orientation, "h")
        # The builder reverses the input so the top of the chart
        # carries the largest count -- visual "top of the list" matches
        # the read model's descending sort.
        self.assertEqual(tuple(bar.y), ("validating", "implementing"))
        self.assertEqual(tuple(bar.x), (4, 10))

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.stage_bars([])
        self.assertEqual(len(fig.data), 0)
        self.assertGreaterEqual(len(fig.layout.annotations), 1)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class ReviewRoundBarsTest(unittest.TestCase):

    def test_groups_rows_by_review_round(self) -> None:
        rows = [
            _agent_exit(review_round=0),
            _agent_exit(review_round=1),
            _agent_exit(review_round=1),
            _agent_exit(review_round=None),  # pre-review pass
        ]
        fig = dashboard_charts.review_round_bars(rows)
        self.assertEqual(len(fig.data), 1)
        bar = fig.data[0]
        # `None` review rounds are bucketed under "0" so they still
        # appear; sorting is numeric ascending.
        self.assertEqual(tuple(bar.x), ("0", "1"))
        self.assertEqual(tuple(bar.y), (2, 2))

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.review_round_bars([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class RepoBarsTest(unittest.TestCase):

    def test_aggregates_per_repo(self) -> None:
        rows = [
            IssueSummaryRow(
                repo="acme/a", issue=1, event_count=10,
                first_seen=datetime(2026, 5, 1),
                last_seen=datetime(2026, 5, 2),
                latest_stage="implementing", agent_exits=2,
                total_cost_usd=0.10, total_input_tokens=10,
                total_output_tokens=5,
            ),
            IssueSummaryRow(
                repo="acme/a", issue=2, event_count=4,
                first_seen=datetime(2026, 5, 3),
                last_seen=datetime(2026, 5, 3),
                latest_stage="validating", agent_exits=1,
                total_cost_usd=0.02, total_input_tokens=5,
                total_output_tokens=2,
            ),
            IssueSummaryRow(
                repo="acme/b", issue=1, event_count=2,
                first_seen=datetime(2026, 5, 4),
                last_seen=datetime(2026, 5, 4),
                latest_stage="ready", agent_exits=0,
                total_cost_usd=None, total_input_tokens=0,
                total_output_tokens=0,
            ),
        ]
        fig = dashboard_charts.repo_bars(rows)
        # Two grouped traces (issues, events).
        self.assertEqual(len(fig.data), 2)
        issue_trace, event_trace = fig.data
        self.assertEqual(issue_trace.name, "issues")
        self.assertEqual(event_trace.name, "events")
        # Repos sorted by total events DESC (acme/a beats acme/b).
        self.assertEqual(tuple(issue_trace.x), ("acme/a", "acme/b"))
        self.assertEqual(tuple(issue_trace.y), (2, 1))
        self.assertEqual(tuple(event_trace.y), (14, 2))
        self.assertEqual(fig.layout.barmode, "group")

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.repo_bars([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class CostCoverageTest(unittest.TestCase):

    def test_donut_slices_by_cost_source(self) -> None:
        rows = [
            _agent_exit(cost_source="reported"),
            _agent_exit(cost_source="reported"),
            _agent_exit(cost_source="estimated"),
            _agent_exit(cost_source=None),  # legacy / unknown
        ]
        fig = dashboard_charts.cost_coverage(rows)
        self.assertEqual(len(fig.data), 1)
        pie = fig.data[0]
        # `None` rows bucketed under "unknown" so they still render.
        self.assertEqual(
            set(pie.labels), {"reported", "estimated", "unknown"}
        )
        by_label = dict(zip(pie.labels, pie.values))
        self.assertEqual(by_label["reported"], 2)
        self.assertEqual(by_label["estimated"], 1)
        self.assertEqual(by_label["unknown"], 1)

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.cost_coverage([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class ThroughputTest(unittest.TestCase):

    def test_sums_daily_counts_across_events(self) -> None:
        points = [
            TimeSeriesPoint(day=date(2026, 5, 1), event="agent_exit", count=2),
            TimeSeriesPoint(day=date(2026, 5, 1), event="stage_enter", count=3),
            TimeSeriesPoint(day=date(2026, 5, 2), event="agent_exit", count=4),
        ]
        fig = dashboard_charts.throughput(points)
        self.assertEqual(len(fig.data), 1)
        bar = fig.data[0]
        self.assertEqual(tuple(bar.x), (date(2026, 5, 1), date(2026, 5, 2)))
        self.assertEqual(tuple(bar.y), (5, 4))

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.throughput([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class Heatmap7x24Test(unittest.TestCase):

    def test_buckets_by_weekday_and_hour(self) -> None:
        # 2026-05-04 is a Monday (weekday()=0); 2026-05-06 is a
        # Wednesday (weekday()=2). The matrix should carry one event
        # at Mon 09 and two at Wed 14.
        timestamps = [
            datetime(2026, 5, 4, 9, 0),
            datetime(2026, 5, 6, 14, 0),
            datetime(2026, 5, 6, 14, 30),
        ]
        fig = dashboard_charts.heatmap_7x24(timestamps)
        self.assertEqual(len(fig.data), 1)
        hm = fig.data[0]
        z = [list(row) for row in hm.z]
        self.assertEqual(len(z), 7)
        self.assertEqual(len(z[0]), 24)
        self.assertEqual(z[0][9], 1)
        self.assertEqual(z[2][14], 2)
        # All other Wed cells stay zero.
        self.assertEqual(z[2][13], 0)
        self.assertEqual(z[2][15], 0)

    def test_empty_input_still_renders_grid_with_annotation(self) -> None:
        # A populated empty grid (all zeros) plus an explicit
        # placeholder annotation lets the operator distinguish "no
        # rows" from "chart never rendered".
        fig = dashboard_charts.heatmap_7x24([])
        self.assertEqual(len(fig.data), 1)
        z = [list(row) for row in fig.data[0].z]
        self.assertTrue(all(cell == 0 for row in z for cell in row))
        self.assertGreaterEqual(len(fig.layout.annotations), 1)


if __name__ == "__main__":
    unittest.main()
