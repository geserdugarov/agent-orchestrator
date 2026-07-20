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

import importlib
import os
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path

try:
    from orchestrator import dashboard_charts
    from orchestrator import dashboard_theme as theme
    from orchestrator.analytics.read import (
        HourlyHeatmapPoint,
        RepoBreakdownRow,
        ReviewRoundBucketRow,
        StageBreakdown,
        ThroughputDayRow,
        TimeSeriesPoint,
    )
    HAS_PLOTLY = True
except ModuleNotFoundError:
    HAS_PLOTLY = False
    dashboard_charts = None  # type: ignore[assignment]


_SKIP_REASON = "plotly not installed -- run `uv sync --group dashboard`"

EVENT_AGENT_EXIT = "agent_exit"
STAGE_IMPLEMENTING = "implementing"
STAGE_VALIDATING = "validating"
ROLE_DEVELOPER = "developer"
ROLE_REVIEWER = "reviewer"
BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
TRACE_CACHE = "Cache"
TRACE_COST = "Cost"
RGBA_PREFIX = "rgba("
EXPECTED_RGBA_MESSAGE = "expected rgba() cache shade, got "
TWO_RUNS_LABEL = "2 runs"

# Fixture days sit inside a single May window; naming them keeps the
# repeated `date(...)` calls out of the per-test bodies.
_YEAR = 2026
_DAY1 = date(_YEAR, 5, 1)
_DAY2 = date(_YEAR, 5, 2)
_DAY3 = date(_YEAR, 5, 3)
_DAY4 = date(_YEAR, 5, 4)
_DAY5 = date(_YEAR, 5, 5)

# Card heights (px) the builders pin so panels do not collapse to
# Plotly's 450px default; these recur across the height-contract tests.
_HERO_HEIGHT = 330
_PLACEHOLDER_HEIGHT = 120
_THROUGHPUT_HEIGHT = 150
_HEATMAP_HEIGHT = 240


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class UsageOverTimeTest(unittest.TestCase):
    """The hero stacked-area chart pivots `TimeSeriesPoint`s into a
    per-day `(input, output, cost)` table and stacks input + output
    token bands with the cost line on a secondary axis.
    """

    def test_stacks_tokens_with_cost_overlay(self) -> None:
        points = [
            TimeSeriesPoint(
                day=_DAY1, event=EVENT_AGENT_EXIT, count=2,
                cost_usd=1.2, input_tokens=1000, output_tokens=500,
                cache_read_tokens=400, cache_write_tokens=200,
            ),
            TimeSeriesPoint(
                day=_DAY2, event=EVENT_AGENT_EXIT, count=3,
                cost_usd=2.4, input_tokens=2000, output_tokens=800,
                cache_read_tokens=900, cache_write_tokens=600,
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
                day=_DAY1, event=EVENT_AGENT_EXIT, count=2,
                cost_usd=0.5, input_tokens=500, output_tokens=200,
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


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class CostHorizontalBarsTest(unittest.TestCase):

    def test_sorts_by_cost_descending(self) -> None:
        cost_rows = [
            ("alpha", "1 run", 5.0, "#111"),
            ("beta", TWO_RUNS_LABEL, 15.0, "#222"),
            ("gamma", "3 runs", 10.0, "#333"),
        ]
        fig = dashboard_charts.cost_horizontal_bars(cost_rows)
        # The builder reverses the input so the LARGEST cost sits at
        # the top of the chart (Plotly draws the first y at the
        # bottom). Pull the y labels back out and check the order.
        y_labels = list(fig.data[0].y)
        # Highest cost (beta) should be the last entry returned by
        # Plotly's bottom-up draw, i.e. the top of the chart.
        self.assertIn("beta", y_labels[-1])
        self.assertIn("gamma", y_labels[-2])

    def test_accepts_items_keyword(self) -> None:
        # `items` is the public keyword; callers may pass the rows by name.
        fig = dashboard_charts.cost_horizontal_bars(
            items=[("alpha", "1 run", 5.0, "#111")],
        )
        self.assertIsNotNone(fig)

    def test_value_labels_render_with_money_shorthand(self) -> None:
        cost_rows = [("repo", "10 events", 12_345.0, "#abc")]
        fig = dashboard_charts.cost_horizontal_bars(cost_rows)
        # `fmt_money` collapses 12_345 to `$12.3K`.
        self.assertEqual(tuple(fig.data[0].text), ("$12.3K",))

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.cost_horizontal_bars([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        # Empty horizontal-bar cards still pin a height matching the
        # single-row non-empty case (40 * 1 + 80) so they do not
        # collapse to Plotly's 450px default.
        self.assertEqual(fig.layout.height, _PLACEHOLDER_HEIGHT)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class CostByStageTest(unittest.TestCase):

    def test_stacks_no_cache_and_cache_per_stage(self) -> None:
        # Each stage splits into (no-cache portion, cache portion).
        # The read model guarantees no_cache + cache == total cost,
        # so the stacked segments add back to the per-stage total.
        rows = [
            StageBreakdown(
                stage=STAGE_IMPLEMENTING,
                count=20,
                total_cost_usd=12.0,
                runs=8,
                cache_cost_usd=9.0,
                no_cache_cost_usd=3.0,
            ),
            StageBreakdown(
                stage=STAGE_VALIDATING,
                count=5,
                total_cost_usd=4.0,
                runs=3,
                cache_cost_usd=1.0,
                no_cache_cost_usd=3.0,
            ),
        ]
        fig = dashboard_charts.cost_by_stage(rows)
        no_cache_trace, cache_trace = fig.data
        # Two traces (no-cache base, cache outer), two bars per trace
        # (one per stage). No-cache is added first so cache stacks
        # outward; the chart stacks under `barmode="stack"`.
        self.assertEqual(len(fig.data), 2)
        self.assertEqual([trace.name for trace in fig.data], ["No cache", TRACE_CACHE])
        self.assertEqual(fig.layout.barmode, "stack")
        # Two bars per trace: one per stage.
        self.assertEqual(len(no_cache_trace.y), 2)
        self.assertEqual(len(cache_trace.y), 2)
        self._assert_stage_bars(no_cache_trace, cache_trace)

    def test_cache_segment_uses_lighter_stage_color(self) -> None:
        # Cache and no-cache must stay visibly paired by stage, so the
        # cache segment is a translucent shade of the stage's base
        # color rather than a separate palette.
        rows = [
            StageBreakdown(
                stage=STAGE_IMPLEMENTING,
                count=10,
                total_cost_usd=10.0,
                runs=4,
                cache_cost_usd=6.0,
                no_cache_cost_usd=4.0,
            ),
        ]
        fig = dashboard_charts.cost_by_stage(rows)
        stage_color = theme.color_for(
            STAGE_IMPLEMENTING, explicit=theme.STAGE_COLORS,
        )
        base_colors, cache_colors = [trace.marker.color for trace in fig.data]
        # No-cache uses the canonical stage color verbatim.
        self.assertEqual(base_colors[0], stage_color)
        # Cache uses an rgba() shade of the same color.
        self.assertTrue(
            cache_colors[0].startswith(RGBA_PREFIX),
            "{0}{1}".format(EXPECTED_RGBA_MESSAGE, cache_colors[0]),
        )

    def test_legacy_rows_plot_full_token_total(self) -> None:
        # Fixtures predating the cache-split read model leave
        # `cache_cost_usd` / `no_cache_cost_usd` at the dataclass
        # default of 0.0; falling through would render an empty bar.
        # The chart falls back to plotting the full total as no-cache
        # so the bar length still reads correctly.
        rows = [
            StageBreakdown(
                stage=STAGE_IMPLEMENTING,
                count=10,
                total_cost_usd=7.5,
                runs=3,
            ),
        ]
        fig = dashboard_charts.cost_by_stage(rows)
        no_cache_trace, cache_trace = fig.data
        self.assertEqual(list(no_cache_trace.x), [7.5])
        self.assertEqual(list(cache_trace.x), [0.0])

    def test_sub_line_labels_runs_not_events(self) -> None:
        # The standalone mock aggregates per-agent-run records and
        # labels the sub-line "runs"; we mirror that by reading
        # `StageBreakdown.runs` (the agent-exit subset of `.count`)
        # so a stage with 20 events but only 8 agent runs reports
        # "8 runs", not "20 events".
        rows = [
            StageBreakdown(
                stage=STAGE_IMPLEMENTING,
                count=20,
                total_cost_usd=12.0,
                runs=8,
                cache_cost_usd=9.0,
                no_cache_cost_usd=3.0,
            ),
        ]
        fig = dashboard_charts.cost_by_stage(rows)
        joined = " ".join(fig.data[0].y)
        self.assertIn("8 runs", joined)
        self.assertNotIn("events", joined)

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.cost_by_stage([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        self.assertEqual(fig.layout.height, _PLACEHOLDER_HEIGHT)

    def _assert_stage_bars(self, no_cache_trace, cache_trace) -> None:
        # Each stage label appears on the shared y-axis.
        for stage in (STAGE_IMPLEMENTING, STAGE_VALIDATING):
            self.assertTrue(
                any(stage in label for label in no_cache_trace.y),
                "stage {0!r} missing from y labels".format(stage),
            )
        # Largest total ($12) sits at the top, but Plotly's horizontal
        # bar draws the first y-value at the bottom, so the arrays are
        # reversed to [validating, implementing] render order. No-cache
        # [validating 3, implementing 3]; cache [validating 1,
        # implementing 9].
        self.assertEqual(list(no_cache_trace.x), [3.0, 3.0])
        self.assertEqual(list(cache_trace.x), [1.0, 9.0])
        # Only the outer (cache) trace carries the per-stage dollar text
        # so the label lands once per bar instead of on each segment;
        # totals reversed to [validating $4, implementing $12].
        self.assertEqual(no_cache_trace.text, None)
        self.assertEqual(list(cache_trace.text), ["$4.00", "$12"])


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class CostByReviewRoundTest(unittest.TestCase):

    def test_round_labels_use_logical_order(self) -> None:
        rows = [
            ReviewRoundBucketRow(
                bucket="0", runs=12, failed=0, total_cost_usd=40.0,
                developer_runs=7, reviewer_runs=5,
                developer_cost_usd=28.0, reviewer_cost_usd=12.0,
                developer_cache_cost_usd=20.0,
                developer_no_cache_cost_usd=8.0,
                reviewer_cache_cost_usd=9.0,
                reviewer_no_cache_cost_usd=3.0,
            ),
            ReviewRoundBucketRow(
                bucket="1", runs=4, failed=1, total_cost_usd=20.0,
                developer_runs=2, reviewer_runs=2,
                developer_cost_usd=9.0, reviewer_cost_usd=11.0,
                developer_cache_cost_usd=7.0,
                developer_no_cache_cost_usd=2.0,
                reviewer_cache_cost_usd=8.0,
                reviewer_no_cache_cost_usd=3.0,
            ),
            ReviewRoundBucketRow(
                bucket="3", runs=2, failed=2, total_cost_usd=15.0,
                developer_runs=1, reviewer_runs=1,
                developer_cost_usd=6.0, reviewer_cost_usd=9.0,
                developer_cache_cost_usd=6.0,
                developer_no_cache_cost_usd=0.0,
                reviewer_cache_cost_usd=9.0,
                reviewer_no_cache_cost_usd=0.0,
            ),
            ReviewRoundBucketRow(
                bucket="unknown", runs=1, failed=0, total_cost_usd=5.0,
                developer_runs=1, reviewer_runs=0,
                developer_cost_usd=5.0, reviewer_cost_usd=0.0,
                developer_cache_cost_usd=0.0,
                developer_no_cache_cost_usd=5.0,
                reviewer_cache_cost_usd=0.0,
                reviewer_no_cache_cost_usd=0.0,
            ),
        ]
        fig = dashboard_charts.cost_by_review_round(rows)
        traces = fig.data
        # Four traces: Review (no cache), Review (cache), Development
        # (no cache), Development (cache). Review is added first so the
        # visible role order per round reads Development above Review;
        # within each role the no-cache trace stacks below cache.
        self.assertEqual(len(traces), 4)
        self.assertEqual(
            [trace.name for trace in traces],
            [
                "Review (no cache)",
                "Review (cache)",
                "Development (no cache)",
                "Development (cache)",
            ],
        )
        self._assert_role_stacking(traces, fig.layout)
        self._assert_round_labels(traces[0].y)
        self._assert_reversed_x(traces)
        self._assert_role_total_text(traces)

    def test_cache_segment_uses_lighter_role_color(self) -> None:
        # Cache and no-cache must stay visibly paired by role, so the
        # cache segment is a translucent shade of the role's base
        # color rather than a separate palette.
        rows = [
            ReviewRoundBucketRow(
                bucket="0", runs=4, failed=0, total_cost_usd=20.0,
                developer_runs=2, reviewer_runs=2,
                developer_cost_usd=10.0, reviewer_cost_usd=10.0,
                developer_cache_cost_usd=6.0,
                developer_no_cache_cost_usd=4.0,
                reviewer_cache_cost_usd=7.0,
                reviewer_no_cache_cost_usd=3.0,
            ),
        ]
        fig = dashboard_charts.cost_by_review_round(rows)
        role_colors = [trace.marker.color for trace in fig.data]
        # The no-cache base traces (indices 0 and 2) use the canonical
        # reviewer / developer role color verbatim.
        self.assertEqual(
            role_colors[0], theme.AGENT_ROLE_COLORS[ROLE_REVIEWER],
        )
        self.assertEqual(
            role_colors[2], theme.AGENT_ROLE_COLORS[ROLE_DEVELOPER],
        )
        # Each cache trace (indices 1 and 3) uses an rgba() shade of
        # its role color rather than a separate palette entry.
        for cache_index in (1, 3):
            self.assertTrue(
                role_colors[cache_index].startswith(RGBA_PREFIX),
                "{0}{1}".format(EXPECTED_RGBA_MESSAGE, role_colors[cache_index]),
            )

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.cost_by_review_round([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        self.assertEqual(fig.layout.height, _PLACEHOLDER_HEIGHT)

    def _assert_role_stacking(self, traces, layout) -> None:
        # `offsetgroup` separates Development from Review (side by side),
        # while same-role traces share an offsetgroup so they stack at
        # the same y bucket under `barmode="relative"`.
        self.assertEqual(layout.barmode, "relative")
        expected_offsetgroups = [
            ROLE_REVIEWER, ROLE_REVIEWER, ROLE_DEVELOPER, ROLE_DEVELOPER,
        ]
        for trace, offsetgroup in zip(traces, expected_offsetgroups):
            self.assertEqual(trace.offsetgroup, offsetgroup)
            self.assertEqual(len(trace.y), 4)
        self.assertEqual(layout.legend.traceorder, "reversed")

    def _assert_round_labels(self, y_labels) -> None:
        joined = " ".join(y_labels)
        for needle in (
            "Initial", "Round 1", "Round 3", "No review round",
            "7 dev / 5 review runs",
        ):
            self.assertIn(needle, joined)

    def _assert_reversed_x(self, traces) -> None:
        # Logical order before Plotly's horizontal-bar reversal is
        # Initial -> Round 1 -> Round 3 -> No review round, so each
        # rendered x array is the reverse of that. Per trace:
        #   Reviewer no-cache: initial 3, 1 -> 3, 3 -> 0, unknown 0.
        #   Reviewer cache:    initial 9, 1 -> 8, 3 -> 9, unknown 0.
        #   Developer no-cache: initial 8, 1 -> 2, 3 -> 0, unknown 5.
        #   Developer cache:    initial 20, 1 -> 7, 3 -> 6, unknown 0.
        expected_x = [
            [0.0, 0.0, 3.0, 3.0],
            [0.0, 9.0, 8.0, 9.0],
            [5.0, 0.0, 2.0, 8.0],
            [0.0, 6.0, 7.0, 20.0],
        ]
        for trace, x_values in zip(traces, expected_x):
            self.assertEqual(list(trace.x), x_values)

    def _assert_role_total_text(self, traces) -> None:
        # Only the cache (outer) segments carry the per-role total text
        # so the dollar label lands once per role bar; the no-cache base
        # traces (indices 0 and 2) leave text unset.
        for no_cache_index in (0, 2):
            self.assertEqual(traces[no_cache_index].text, None)
        # Cache-trace text reversed to render order (per `fmt_money`:
        # values below $10 keep cents, $10+ rounds to whole dollars).
        # Review then Developer per-round totals.
        review_totals = ["$0.00", "$9.00", "$11", "$12"]
        developer_totals = ["$5.00", "$6.00", "$9.00", "$28"]
        self.assertEqual(list(traces[1].text), review_totals)
        self.assertEqual(list(traces[3].text), developer_totals)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class CostByRepoTest(unittest.TestCase):

    def test_strips_owner_prefix_for_legibility(self) -> None:
        rows = [
            RepoBreakdownRow(
                repo="acme/widgets", issues=2, events=10,
                agent_exits=4, total_cost_usd=8.0,
            ),
            RepoBreakdownRow(
                repo="acme/gadgets", issues=1, events=4,
                agent_exits=2, total_cost_usd=3.0,
            ),
        ]
        fig = dashboard_charts.cost_by_repo(rows)
        joined = " ".join(fig.data[0].y)
        # The short name is what the operator reads; the full
        # `owner/name` slug stays in the read model but not the chart
        # label.
        self.assertIn("widgets", joined)
        self.assertIn("gadgets", joined)
        # Sub-line carries the per-repo agent-run count, matching the
        # standalone mock's per-run aggregation; counting every event
        # would overstate per-repo activity against the per-run cost.
        self.assertIn("4 runs", joined)
        self.assertIn(TWO_RUNS_LABEL, joined)
        self.assertNotIn("events", joined)

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.cost_by_repo([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        self.assertEqual(fig.layout.height, _PLACEHOLDER_HEIGHT)


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
                weekday=0, hour=9, count=1, total_tokens=1_500,
            ),
            HourlyHeatmapPoint(
                weekday=3, hour=14, count=5, total_tokens=12_000,
            ),
        ]
        fig = dashboard_charts.hour_weekday_heatmap(points)
        grid = self._cell_grid(fig)
        self.assertEqual(len(grid), 7)
        self.assertEqual(len(grid[0]), 24)
        self.assertEqual(grid[0][9], 1_500)
        self.assertEqual(grid[3][14], 12_000)

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
                day=_DAY1, event=EVENT_AGENT_EXIT, count=1,
                cost_usd=1.0, input_tokens=10, output_tokens=10,
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
        self.assertEqual(fig.layout.height, 40 * 3 + 80)

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


# Each chart family's public builders live in a focused leaf module;
# the `orchestrator.dashboard_charts` hub re-exports each so
# `dashboard_charts.<builder>` keeps resolving to the same object.
_BASE_LEAF = "orchestrator.dashboard_charts_base"
_COST_LEAF = "orchestrator.dashboard_charts_cost"
_USAGE_LEAF = "orchestrator.dashboard_charts_usage"
_HEATMAP_LEAF = "orchestrator.dashboard_charts_heatmap"
_THROUGHPUT_LEAF = "orchestrator.dashboard_charts_throughput"

_CHART_LEAVES = (
    _BASE_LEAF, _COST_LEAF, _USAGE_LEAF, _HEATMAP_LEAF, _THROUGHPUT_LEAF,
)

_REPO_ROOT = str(Path(__file__).resolve().parents[1])

# public builder -> owning leaf module
_BUILDER_HOMES = (
    ("cost_horizontal_bars", _COST_LEAF),
    ("cost_by_repo", _COST_LEAF),
    ("cost_by_stage", _COST_LEAF),
    ("cost_by_review_round", _COST_LEAF),
    ("usage_over_time", _USAGE_LEAF),
    ("backend_per_day", _USAGE_LEAF),
    ("hour_weekday_heatmap", _HEATMAP_LEAF),
    ("done_per_day_bars", _THROUGHPUT_LEAF),
)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class DirectLeafImportTest(unittest.TestCase):
    """Every chart leaf imports cleanly in a fresh process, not only when
    `dashboard_charts` is imported first.

    `dashboard_charts` is a compatibility hub that re-exports each family's
    builders, and every chart module takes its shared low-level primitives
    from `dashboard_charts_base`. A direct `import` of any leaf must therefore
    resolve without a partially-initialized-module circular import. A
    subprocess gives the clean import graph the in-process test session --
    which has already imported `dashboard_charts` at collection -- cannot.
    """

    def test_each_leaf_imports_standalone(self) -> None:
        for module in _CHART_LEAVES:
            with self.subTest(module=module):
                self._assert_imports_clean(module)

    def _assert_imports_clean(self, module: str) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=_REPO_ROOT,
            env={
                **os.environ,
                "ORCHESTRATOR_SKIP_DOTENV": "1",
                "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
            },
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"clean-process `import {module}` failed:\n{completed.stderr}",
        )


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class ChartHubExtractionTest(unittest.TestCase):
    """Each chart family's public builders live in a focused leaf module, and
    the `orchestrator.dashboard_charts` hub re-exports each under its original
    name so `dashboard_charts.<builder>` (the widget pipeline and these tests
    reach it) keeps resolving to the same object.
    """

    def test_builders_defined_in_their_leaf(self) -> None:
        for name, module_name in _BUILDER_HOMES:
            with self.subTest(builder=name):
                leaf = importlib.import_module(module_name)
                self.assertEqual(getattr(leaf, name).__module__, module_name)

    def test_hub_reexports_the_leaf_objects(self) -> None:
        from orchestrator import dashboard_charts
        for name, module_name in _BUILDER_HOMES:
            with self.subTest(builder=name):
                leaf = importlib.import_module(module_name)
                self.assertIs(
                    getattr(dashboard_charts, name), getattr(leaf, name)
                )


if __name__ == "__main__":
    unittest.main()
