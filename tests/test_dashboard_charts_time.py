# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard heatmap, throughput, and chart-height tests."""

import importlib


import unittest


from datetime import date

_HOUR_TOKEN_VOLUME_TOTAL_TOKE = 1_500
_HOUR_TOKEN_VOLUME_HOUR = 14
_HOUR_TOKEN_VOLUME_TO_SECONDARY = 12_000
_HOUR_TOKEN_VOLUME = 1_500
_HOUR_TOKEN_VOLUME_ASSERTEQUA = 14
_HOUR_TOKEN_VOLUME_SECONDARY = 12_000
_HEIGHT_SCALES_ROWS_HEIGHT = 40
_HEIGHT_SCALES_ROWS_H_SECONDARY = 80


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


TWO_RUNS_LABEL = "2 runs"


_YEAR = 2026


_DAY1 = date(_YEAR, 5, 1)


_DAY2 = date(_YEAR, 5, 2)


_DAY3 = date(_YEAR, 5, 3)


_DAY4 = date(_YEAR, 5, 4)


_DAY5 = date(_YEAR, 5, 5)


_HERO_HEIGHT = 330


_THROUGHPUT_HEIGHT = 150


_HEATMAP_HEIGHT = 240


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class HourWeekdayHeatmapTest(unittest.TestCase):
    def test_buckets_by_weekday_and_hour_token_volume(self) -> None:
        # The redesigned heatmap renders token volume per cell, not
        # event count -- matching the standalone mock's "Token volume
        # by hour x weekday" framing. Two cells: Sunday 09:00 with
        # 1.5K tokens and Wednesday 14:00 with 12K tokens (event
        # counts of 1 / 5 are deliberately at a different scale).
        points = [
            HourlyHeatmapPoint(
                weekday=0,
                hour=9,
                count=1,
                total_tokens=_HOUR_TOKEN_VOLUME_TOTAL_TOKE,
            ),
            HourlyHeatmapPoint(
                weekday=3,
                hour=_HOUR_TOKEN_VOLUME_HOUR,
                count=5,
                total_tokens=_HOUR_TOKEN_VOLUME_TO_SECONDARY,
            ),
        ]
        fig = dashboard_charts.hour_weekday_heatmap(points)
        grid = self._cell_grid(fig)
        self.assertEqual(len(grid), 7)
        self.assertEqual(len(grid[0]), 24)
        self.assertEqual(grid[0][9], _HOUR_TOKEN_VOLUME)
        self.assertEqual(grid[3][_HOUR_TOKEN_VOLUME_ASSERTEQUA], _HOUR_TOKEN_VOLUME_SECONDARY)

    def test_empty_input_renders_annotated_grid(self) -> None:
        fig = dashboard_charts.hour_weekday_heatmap([])
        grid = self._cell_grid(fig)
        self.assertTrue(all(cell == 0 for row in grid for cell in row))
        self.assertGreaterEqual(len(fig.layout.annotations), 1)

    def test_plot_background_paints_the_cell_grid(self) -> None:
        # The inter-cell gaps show the plot background, so painting it
        # the border colour turns them into a visible weekday x hour
        # grid -- otherwise zero-volume (white) cells vanish against a
        # white backdrop and the sparse hours read as missing data.
        fig = dashboard_charts.hour_weekday_heatmap([])
        heatmap = fig.data[0]
        self.assertEqual(fig.layout.plot_bgcolor, theme.BORDER)
        self.assertGreater(heatmap.xgap, 0)
        self.assertGreater(heatmap.ygap, 0)

    def test_x_axis_label_defaults_to_utc(self) -> None:
        fig = dashboard_charts.hour_weekday_heatmap([])
        self.assertEqual(fig.layout.xaxis.title.text, "hour (UTC)")

    def test_x_axis_label_reflects_tz_label(self) -> None:
        fig = dashboard_charts.hour_weekday_heatmap([], tz_label="UTC+7")
        self.assertEqual(fig.layout.xaxis.title.text, "hour (UTC+7)")

    def _cell_grid(self, fig) -> list:
        # The single heatmap trace's z-matrix as a plain nested list of
        # weekday (7) x hour (24) token-volume cells.
        return [list(row) for row in fig.data[0].z]


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class DonePerDayBarsTest(unittest.TestCase):
    def test_reads_resolved_column(self) -> None:
        rows = [
            ThroughputDayRow(day=_DAY1, resolved=2, rejected=0),
            ThroughputDayRow(day=_DAY2, resolved=4, rejected=1),
        ]
        fig = dashboard_charts.done_per_day_bars(rows)
        bars = self._resolved_bars(fig)
        self.assertEqual(tuple(bars.x), (_DAY1, _DAY2))
        self.assertEqual(tuple(bars.y), (2, 4))

    def test_window_backfills_zero_resolved_days(self) -> None:
        # SQL only returns days with `done` / `rejected` rows, so
        # zero-resolved days in the middle of the selected window
        # would otherwise be silently absent. With an explicit window
        # we render every day -- including the empty ones -- so the
        # operator sees a continuous calendar baseline.
        rows = [
            ThroughputDayRow(day=_DAY1, resolved=2, rejected=0),
            ThroughputDayRow(day=_DAY4, resolved=3, rejected=1),
        ]
        fig = dashboard_charts.done_per_day_bars(
            rows,
            window_start=_DAY1,
            window_end=_DAY5,
        )
        bars = self._resolved_bars(fig)
        self.assertEqual(
            tuple(bars.x),
            (_DAY1, _DAY2, _DAY3, _DAY4, _DAY5),
        )
        # Zero-resolved days surface as explicit zero bars rather
        # than being elided from the x-axis.
        self.assertEqual(tuple(bars.y), (2, 0, 0, 3, 0))

    def test_empty_window_renders_zero_baseline(self) -> None:
        # A window with no resolved issues at all renders an all-zero
        # baseline rather than the placeholder annotation, so the
        # operator can still see the calendar drawn out for the
        # selected range.
        fig = dashboard_charts.done_per_day_bars(
            [],
            window_start=_DAY1,
            window_end=_DAY3,
        )
        bars = self._resolved_bars(fig)
        self.assertEqual(tuple(bars.x), (_DAY1, _DAY2, _DAY3))
        self.assertEqual(tuple(bars.y), (0, 0, 0))

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.done_per_day_bars([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        # Empty throughput strip still pins the thin-strip height
        # instead of collapsing back to Plotly's 450px default.
        self.assertEqual(fig.layout.height, _THROUGHPUT_HEIGHT)

    def _resolved_bars(self, fig):
        # The single bar trace done_per_day_bars renders: resolved-issue
        # counts keyed by day.
        return fig.data[0]


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class ChartHeightsTest(unittest.TestCase):
    """Every builder pins an explicit ``layout.height`` so the cards
    do not float at Plotly's 450px default. Each value is tuned to
    the panel's content shape (hero / horizontal bars / heatmap /
    throughput strip); the visual-review task #341 follow-up pinned
    these heights as the single biggest "now it looks designed"
    lever after the segmented control.
    """

    def test_hero_chart_height_matches_mock(self) -> None:
        points = [
            TimeSeriesPoint(
                day=_DAY1,
                event=EVENT_AGENT_EXIT,
                count=1,
                cost_usd=1.0,
                input_tokens=10,
                output_tokens=10,
            ),
        ]
        fig = dashboard_charts.usage_over_time(points)
        self.assertEqual(fig.layout.height, _HERO_HEIGHT)

    def test_horizontal_bars_height_scales_with_rows(self) -> None:
        # Three bars: ~40px per row + 80 = 200.
        cost_rows = [
            ("alpha", "1 run", 1.0, "#111"),
            ("beta", TWO_RUNS_LABEL, 2.0, "#222"),
            ("gamma", "3 runs", 3.0, "#333"),
        ]
        fig = dashboard_charts.cost_horizontal_bars(cost_rows)
        self.assertEqual(fig.layout.height, _HEIGHT_SCALES_ROWS_HEIGHT * 3 + _HEIGHT_SCALES_ROWS_H_SECONDARY)

    def test_done_per_day_strip_height(self) -> None:
        rows = [
            ThroughputDayRow(day=_DAY1, resolved=1, rejected=0),
        ]
        fig = dashboard_charts.done_per_day_bars(rows)
        # Throughput strip lives in the narrow reliability column; the
        # thin height keeps it from dwarfing the tiles above it.
        self.assertEqual(fig.layout.height, _THROUGHPUT_HEIGHT)

    def test_heatmap_height_matches_mock_squares(self) -> None:
        # 7 rows x 24 columns: the standalone mock's compact square
        # cells need the pinned height, not Plotly's default 450.
        fig = dashboard_charts.hour_weekday_heatmap([])
        self.assertEqual(fig.layout.height, _HEATMAP_HEIGHT)
