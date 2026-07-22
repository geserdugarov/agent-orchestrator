# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard usage-over-time chart tests."""

import importlib


import unittest


from datetime import date

_TOKENS_COST_OVERLAY_COST_USD = 1.2
_TOKENS_COST_OVERLAY_OUTPUT_T = 500
_TOKENS_COST_OVERLAY_CACHE_RE = 400
_TOKENS_COST_OVERLAY_CACHE_WR = 200
_TOKENS_COST_OVERLAY_SECONDARY = 2.4
_TOKENS_COST_OVERLAY_INPUT_TO = 2000
_TOKENS_COST_OVERLAY_O_TERTIARY = 800
_TOKENS_COST_OVERLAY_C_TERTIARY = 900
_TOKENS_COST_OVERLAY_QUATERNARY = 600
_STACKS_PER_BACKEND_INPUT_TOK = 500
_STACKS_PER_BACKEND_OUTPUT_TO = 200


def _load_chart_dependencies():
    charts = importlib.import_module("orchestrator.dashboard_charts")
    theme_module = importlib.import_module("orchestrator.dashboard_theme")
    read_module = importlib.import_module("orchestrator.analytics.read")
    return charts, theme_module, read_module


try:
    dashboard_charts, theme, _analytics_read = _load_chart_dependencies()
except ModuleNotFoundError:
    HAS_PLOTLY = False
    dashboard_charts = None  # type: ignore[assignment]
else:
    HAS_PLOTLY = True
    HourlyHeatmapPoint = _analytics_read.HourlyHeatmapPoint
    RepoBreakdownRow = _analytics_read.RepoBreakdownRow
    ReviewRoundBucketRow = _analytics_read.ReviewRoundBucketRow
    StageBreakdown = _analytics_read.StageBreakdown
    ThroughputDayRow = _analytics_read.ThroughputDayRow
    TimeSeriesPoint = _analytics_read.TimeSeriesPoint


_SKIP_REASON = "plotly not installed -- run `uv sync --group dashboard`"


EVENT_AGENT_EXIT = "agent_exit"


BACKEND_CLAUDE = "claude"


BACKEND_CODEX = "codex"


TRACE_CACHE = "Cache"


TRACE_COST = "Cost"


_YEAR = 2026


_DAY1 = date(_YEAR, 5, 1)


_DAY2 = date(_YEAR, 5, 2)


_HERO_HEIGHT = 330


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class UsageOverTimeTest(unittest.TestCase):
    """The hero stacked-area chart pivots `TimeSeriesPoint`s into a
    per-day `(input, output, cost)` table and stacks input + output
    token bands with the cost line on a secondary axis.
    """

    def test_stacks_tokens_with_cost_overlay(self) -> None:
        points = [
            TimeSeriesPoint(
                day=_DAY1,
                event=EVENT_AGENT_EXIT,
                count=2,
                cost_usd=_TOKENS_COST_OVERLAY_COST_USD,
                input_tokens=1000,
                output_tokens=_TOKENS_COST_OVERLAY_OUTPUT_T,
                cache_read_tokens=_TOKENS_COST_OVERLAY_CACHE_RE,
                cache_write_tokens=_TOKENS_COST_OVERLAY_CACHE_WR,
            ),
            TimeSeriesPoint(
                day=_DAY2,
                event=EVENT_AGENT_EXIT,
                count=3,
                cost_usd=_TOKENS_COST_OVERLAY_SECONDARY,
                input_tokens=_TOKENS_COST_OVERLAY_INPUT_TO,
                output_tokens=_TOKENS_COST_OVERLAY_O_TERTIARY,
                cache_read_tokens=_TOKENS_COST_OVERLAY_C_TERTIARY,
                cache_write_tokens=_TOKENS_COST_OVERLAY_QUATERNARY,
            ),
        ]
        fig = dashboard_charts.usage_over_time(points)
        # Three stacked area bands (Input, Output, Cache) plus the
        # cost line; the Cache band totals cache_read + cache_write
        # per day (the standalone mock's `r.cr + r.cw` accounting).
        names = [trace.name for trace in fig.data]
        self.assertIn("Input", names)
        self.assertIn("Output", names)
        self.assertIn(TRACE_CACHE, names)
        self.assertIn(TRACE_COST, names)
        cache_trace = next(trace for trace in fig.data if trace.name == TRACE_CACHE)
        self.assertEqual(tuple(cache_trace.y), (600, 1500))
        cost_trace = next(trace for trace in fig.data if trace.name == TRACE_COST)
        # Cost rides the secondary axis so it can use $ ticks.
        self.assertEqual(cost_trace.yaxis, "y2")

    def test_backend_mode_stacks_per_backend(self) -> None:
        points = [
            TimeSeriesPoint(
                day=_DAY1,
                event=EVENT_AGENT_EXIT,
                count=2,
                cost_usd=0.5,
                input_tokens=_STACKS_PER_BACKEND_INPUT_TOK,
                output_tokens=_STACKS_PER_BACKEND_OUTPUT_TO,
            ),
        ]
        backend_by_day = {
            _DAY1: {BACKEND_CLAUDE: 1200, BACKEND_CODEX: 600},
        }
        fig = dashboard_charts.usage_over_time(
            points,
            backend_rows_by_day=backend_by_day,
            mode="backend",
        )
        names = {trace.name for trace in fig.data}
        # Backend bands plus the cost overlay.
        self.assertIn(BACKEND_CLAUDE, names)
        self.assertIn(BACKEND_CODEX, names)
        self.assertIn(TRACE_COST, names)

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.usage_over_time([])
        self.assertEqual(len(fig.data), 0)
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        # Empty cards must still pin the hero-chart height; without it
        # a "no events" state collapses back to Plotly's 450px default
        # and dwarfs the surrounding KPI strip.
        self.assertEqual(fig.layout.height, _HERO_HEIGHT)
