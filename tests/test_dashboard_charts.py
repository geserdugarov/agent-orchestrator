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
from datetime import date

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


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class UsageOverTimeTest(unittest.TestCase):
    """The hero stacked-area chart pivots `TimeSeriesPoint`s into a
    per-day `(input, output, cost)` table and stacks input + output
    token bands with the cost line on a secondary axis.
    """

    def test_stacks_input_output_cache_with_cost_overlay(self) -> None:
        points = [
            TimeSeriesPoint(
                day=date(2026, 5, 1), event="agent_exit", count=2,
                cost_usd=1.20, input_tokens=1000, output_tokens=500,
                cache_read_tokens=400, cache_write_tokens=200,
            ),
            TimeSeriesPoint(
                day=date(2026, 5, 2), event="agent_exit", count=3,
                cost_usd=2.40, input_tokens=2000, output_tokens=800,
                cache_read_tokens=900, cache_write_tokens=600,
            ),
        ]
        fig = dashboard_charts.usage_over_time(points)
        # Three stacked area bands (Input, Output, Cache) plus the
        # cost line; the Cache band totals cache_read + cache_write
        # per day (the standalone mock's `r.cr + r.cw` accounting).
        names = [t.name for t in fig.data]
        self.assertIn("Input", names)
        self.assertIn("Output", names)
        self.assertIn("Cache", names)
        self.assertIn("Cost", names)
        cache_trace = next(t for t in fig.data if t.name == "Cache")
        self.assertEqual(tuple(cache_trace.y), (600, 1500))
        cost_trace = next(t for t in fig.data if t.name == "Cost")
        # Cost rides the secondary axis so it can use $ ticks.
        self.assertEqual(cost_trace.yaxis, "y2")

    def test_backend_mode_stacks_per_backend(self) -> None:
        points = [
            TimeSeriesPoint(
                day=date(2026, 5, 1), event="agent_exit", count=2,
                cost_usd=0.50, input_tokens=500, output_tokens=200,
            ),
        ]
        backend_by_day = {
            date(2026, 5, 1): {"claude": 1200, "codex": 600},
        }
        fig = dashboard_charts.usage_over_time(
            points,
            backend_rows_by_day=backend_by_day,
            mode="backend",
        )
        names = {t.name for t in fig.data}
        # Backend bands plus the cost overlay.
        self.assertIn("claude", names)
        self.assertIn("codex", names)
        self.assertIn("Cost", names)

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.usage_over_time([])
        self.assertEqual(len(fig.data), 0)
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        # Empty cards must still pin the hero-chart height; without it
        # a "no events" state collapses back to Plotly's 450px default
        # and dwarfs the surrounding KPI strip.
        self.assertEqual(fig.layout.height, 330)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class CostHorizontalBarsTest(unittest.TestCase):

    def test_sorts_by_cost_descending(self) -> None:
        items = [
            ("alpha", "1 run", 5.0, "#111"),
            ("beta", "2 runs", 15.0, "#222"),
            ("gamma", "3 runs", 10.0, "#333"),
        ]
        fig = dashboard_charts.cost_horizontal_bars(items)
        # The builder reverses the input so the LARGEST cost sits at
        # the top of the chart (Plotly draws the first y at the
        # bottom). Pull the y labels back out and check the order.
        y_labels = list(fig.data[0].y)
        # Highest cost (beta) should be the last entry returned by
        # Plotly's bottom-up draw, i.e. the top of the chart.
        self.assertIn("beta", y_labels[-1])
        self.assertIn("gamma", y_labels[-2])

    def test_value_labels_render_with_money_shorthand(self) -> None:
        items = [("repo", "10 events", 12_345.0, "#abc")]
        fig = dashboard_charts.cost_horizontal_bars(items)
        # `fmt_money` collapses 12_345 to `$12.3K`.
        self.assertEqual(tuple(fig.data[0].text), ("$12.3K",))

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.cost_horizontal_bars([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        # Empty horizontal-bar cards still pin a height matching the
        # single-row non-empty case (40 * 1 + 80) so they do not
        # collapse to Plotly's 450px default.
        self.assertEqual(fig.layout.height, 120)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class CostByStageTest(unittest.TestCase):

    def test_stacks_no_cache_and_cache_per_stage(self) -> None:
        # Each stage splits into (no-cache portion, cache portion).
        # The read model guarantees no_cache + cache == total cost,
        # so the stacked segments add back to the per-stage total.
        rows = [
            StageBreakdown(
                stage="implementing",
                count=20,
                total_cost_usd=12.0,
                runs=8,
                cache_cost_usd=9.0,
                no_cache_cost_usd=3.0,
            ),
            StageBreakdown(
                stage="validating",
                count=5,
                total_cost_usd=4.0,
                runs=3,
                cache_cost_usd=1.0,
                no_cache_cost_usd=3.0,
            ),
        ]
        fig = dashboard_charts.cost_by_stage(rows)
        # Two traces (no-cache base, cache outer), two bars per trace
        # (one per stage). No-cache is added first so cache stacks
        # outward; the chart stacks under `barmode="stack"`.
        self.assertEqual(len(fig.data), 2)
        self.assertEqual([t.name for t in fig.data], ["No cache", "Cache"])
        self.assertEqual(fig.layout.barmode, "stack")
        for trace in fig.data:
            self.assertEqual(len(trace.y), 2)
        for stage in ("implementing", "validating"):
            self.assertTrue(
                any(stage in lbl for lbl in fig.data[0].y),
                f"stage {stage!r} missing from y labels",
            )
        # Largest total ($12) sits at the top, but Plotly's horizontal
        # bar draws the first y-value at the bottom, so the y array is
        # reversed: [validating, implementing] in render order.
        # No-cache reversed: [validating: 3, implementing: 3].
        self.assertEqual(list(fig.data[0].x), [3.0, 3.0])
        # Cache reversed: [validating: 1, implementing: 9].
        self.assertEqual(list(fig.data[1].x), [1.0, 9.0])
        # Only the outer (cache) trace carries the per-stage dollar
        # text so the label lands once per bar instead of duplicating
        # on each segment.
        self.assertEqual(fig.data[0].text, None)
        # Total per stage reversed: [validating $4, implementing $12].
        self.assertEqual(list(fig.data[1].text), ["$4.00", "$12"])

    def test_cache_segment_uses_lighter_stage_color(self) -> None:
        # Cache and no-cache must stay visibly paired by stage, so the
        # cache segment is a translucent shade of the stage's base
        # color rather than a separate palette.
        rows = [
            StageBreakdown(
                stage="implementing",
                count=10,
                total_cost_usd=10.0,
                runs=4,
                cache_cost_usd=6.0,
                no_cache_cost_usd=4.0,
            ),
        ]
        fig = dashboard_charts.cost_by_stage(rows)
        stage_color = theme.color_for(
            "implementing", explicit=theme.STAGE_COLORS,
        )
        # No-cache uses the canonical stage color verbatim.
        self.assertEqual(fig.data[0].marker.color[0], stage_color)
        # Cache uses an rgba() shade of the same color.
        self.assertTrue(
            fig.data[1].marker.color[0].startswith("rgba("),
            f"expected rgba() cache shade, got {fig.data[1].marker.color[0]}",
        )

    def test_legacy_rows_without_cache_split_plot_full_total(self) -> None:
        # Fixtures predating the cache-split read model leave
        # `cache_cost_usd` / `no_cache_cost_usd` at the dataclass
        # default of 0.0; falling through would render an empty bar.
        # The chart falls back to plotting the full total as no-cache
        # so the bar length still reads correctly.
        rows = [
            StageBreakdown(
                stage="implementing",
                count=10,
                total_cost_usd=7.5,
                runs=3,
            ),
        ]
        fig = dashboard_charts.cost_by_stage(rows)
        self.assertEqual(list(fig.data[0].x), [7.5])
        self.assertEqual(list(fig.data[1].x), [0.0])

    def test_sub_line_labels_runs_not_events(self) -> None:
        # The standalone mock aggregates per-agent-run records and
        # labels the sub-line "runs"; we mirror that by reading
        # `StageBreakdown.runs` (the agent-exit subset of `.count`)
        # so a stage with 20 events but only 8 agent runs reports
        # "8 runs", not "20 events".
        rows = [
            StageBreakdown(
                stage="implementing",
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
        self.assertEqual(fig.layout.height, 120)


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class CostByReviewRoundTest(unittest.TestCase):

    def test_renders_review_round_labels_in_logical_order(self) -> None:
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
        # Four traces: Review (no cache), Review (cache),
        # Development (no cache), Development (cache). Review is
        # added first so the visible role order per round reads
        # Development above Review. Within each role the no-cache
        # trace stacks below cache (cache reads outward).
        self.assertEqual(len(fig.data), 4)
        self.assertEqual(
            [t.name for t in fig.data],
            [
                "Review (no cache)",
                "Review (cache)",
                "Development (no cache)",
                "Development (cache)",
            ],
        )
        # `offsetgroup` separates Development from Review (side by
        # side), while same-role traces share an offsetgroup so they
        # stack at the same y bucket under `barmode="relative"`.
        self.assertEqual(fig.layout.barmode, "relative")
        self.assertEqual(fig.data[0].offsetgroup, "reviewer")
        self.assertEqual(fig.data[1].offsetgroup, "reviewer")
        self.assertEqual(fig.data[2].offsetgroup, "developer")
        self.assertEqual(fig.data[3].offsetgroup, "developer")
        self.assertEqual(fig.layout.legend.traceorder, "reversed")
        for trace in fig.data:
            self.assertEqual(len(trace.y), 4)
        joined = " ".join(fig.data[0].y)
        for needle in (
            "Initial", "Round 1", "Round 3", "No review round",
            "7 dev / 5 review runs",
        ):
            self.assertIn(needle, joined)
        # Logical order before Plotly's horizontal-bar reversal is
        # Initial -> Round 1 -> Round 3 -> No review round, so the
        # rendered y array is the reverse of that.
        # Reviewer no-cache: [0=initial: 3, 1: 3, 3: 0, unknown: 0]
        # -> reversed = [0, 0, 3, 3].
        self.assertEqual(list(fig.data[0].x), [0.0, 0.0, 3.0, 3.0])
        # Reviewer cache: initial 9, 1 -> 8, 3 -> 9, unknown 0
        # reversed = [0, 9, 8, 9].
        self.assertEqual(list(fig.data[1].x), [0.0, 9.0, 8.0, 9.0])
        # Developer no-cache: initial 8, 1 -> 2, 3 -> 0, unknown 5
        # reversed = [5, 0, 2, 8].
        self.assertEqual(list(fig.data[2].x), [5.0, 0.0, 2.0, 8.0])
        # Developer cache: initial 20, 1 -> 7, 3 -> 6, unknown 0
        # reversed = [0, 6, 7, 20].
        self.assertEqual(list(fig.data[3].x), [0.0, 6.0, 7.0, 20.0])
        # Only the cache (outer) segments carry the per-role total
        # text so the dollar label lands once per role bar; per-role
        # total per round = no_cache + cache.
        self.assertEqual(fig.data[0].text, None)
        self.assertEqual(fig.data[2].text, None)
        # Review totals reversed: initial 12, 1 -> 11, 3 -> 9, unknown 0
        # reversed text values match those role totals (per `fmt_money`:
        # values below $10 keep cents, $10+ rounds to whole dollars).
        self.assertEqual(
            list(fig.data[1].text),
            ["$0.00", "$9.00", "$11", "$12"],
        )
        # Developer totals reversed: initial 28, 1 -> 9, 3 -> 6, unknown 5
        self.assertEqual(
            list(fig.data[3].text),
            ["$5.00", "$6.00", "$9.00", "$28"],
        )

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
        from orchestrator import dashboard_theme as theme
        # Reviewer no-cache uses the canonical role color verbatim.
        self.assertEqual(
            fig.data[0].marker.color, theme.AGENT_ROLE_COLORS["reviewer"],
        )
        # Reviewer cache uses an rgba() shade of the same color.
        self.assertTrue(
            fig.data[1].marker.color.startswith("rgba("),
            f"expected rgba() cache shade, got {fig.data[1].marker.color}",
        )
        self.assertEqual(
            fig.data[2].marker.color, theme.AGENT_ROLE_COLORS["developer"],
        )
        self.assertTrue(
            fig.data[3].marker.color.startswith("rgba("),
            f"expected rgba() cache shade, got {fig.data[3].marker.color}",
        )

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.cost_by_review_round([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        self.assertEqual(fig.layout.height, 120)


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
        self.assertIn("2 runs", joined)
        self.assertNotIn("events", joined)

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.cost_by_repo([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        self.assertEqual(fig.layout.height, 120)


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
        z = [list(row) for row in fig.data[0].z]
        self.assertEqual(len(z), 7)
        self.assertEqual(len(z[0]), 24)
        self.assertEqual(z[0][9], 1_500)
        self.assertEqual(z[3][14], 12_000)

    def test_empty_input_still_renders_grid_with_annotation(self) -> None:
        fig = dashboard_charts.hour_weekday_heatmap([])
        z = [list(row) for row in fig.data[0].z]
        self.assertTrue(all(cell == 0 for row in z for cell in row))
        self.assertGreaterEqual(len(fig.layout.annotations), 1)

    def test_plot_background_paints_the_cell_grid(self) -> None:
        # The inter-cell gaps show the plot background, so painting it
        # the border colour turns them into a visible weekday x hour
        # grid -- otherwise zero-volume (white) cells vanish against a
        # white backdrop and the sparse hours read as missing data.
        fig = dashboard_charts.hour_weekday_heatmap([])
        self.assertEqual(fig.layout.plot_bgcolor, theme.BORDER)
        self.assertGreater(fig.data[0].xgap, 0)
        self.assertGreater(fig.data[0].ygap, 0)

    def test_x_axis_label_defaults_to_utc(self) -> None:
        fig = dashboard_charts.hour_weekday_heatmap([])
        self.assertEqual(fig.layout.xaxis.title.text, "hour (UTC)")

    def test_x_axis_label_reflects_tz_label(self) -> None:
        fig = dashboard_charts.hour_weekday_heatmap([], tz_label="UTC+7")
        self.assertEqual(fig.layout.xaxis.title.text, "hour (UTC+7)")


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class DonePerDayBarsTest(unittest.TestCase):

    def test_reads_resolved_column(self) -> None:
        rows = [
            ThroughputDayRow(day=date(2026, 5, 1), resolved=2, rejected=0),
            ThroughputDayRow(day=date(2026, 5, 2), resolved=4, rejected=1),
        ]
        fig = dashboard_charts.done_per_day_bars(rows)
        bar = fig.data[0]
        self.assertEqual(tuple(bar.x), (date(2026, 5, 1), date(2026, 5, 2)))
        self.assertEqual(tuple(bar.y), (2, 4))

    def test_window_backfills_zero_resolved_days(self) -> None:
        # SQL only returns days with `done` / `rejected` rows, so
        # zero-resolved days in the middle of the selected window
        # would otherwise be silently absent. With an explicit window
        # we render every day -- including the empty ones -- so the
        # operator sees a continuous calendar baseline.
        rows = [
            ThroughputDayRow(day=date(2026, 5, 1), resolved=2, rejected=0),
            ThroughputDayRow(day=date(2026, 5, 4), resolved=3, rejected=1),
        ]
        fig = dashboard_charts.done_per_day_bars(
            rows,
            window_start=date(2026, 5, 1),
            window_end=date(2026, 5, 5),
        )
        bar = fig.data[0]
        self.assertEqual(
            tuple(bar.x),
            (
                date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3),
                date(2026, 5, 4), date(2026, 5, 5),
            ),
        )
        # Zero-resolved days surface as explicit zero bars rather
        # than being elided from the x-axis.
        self.assertEqual(tuple(bar.y), (2, 0, 0, 3, 0))

    def test_window_with_no_rows_still_renders_zero_baseline(self) -> None:
        # A window with no resolved issues at all renders an all-zero
        # baseline rather than the placeholder annotation, so the
        # operator can still see the calendar drawn out for the
        # selected range.
        fig = dashboard_charts.done_per_day_bars(
            [],
            window_start=date(2026, 5, 1),
            window_end=date(2026, 5, 3),
        )
        bar = fig.data[0]
        self.assertEqual(
            tuple(bar.x),
            (date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)),
        )
        self.assertEqual(tuple(bar.y), (0, 0, 0))

    def test_empty_renders_placeholder(self) -> None:
        fig = dashboard_charts.done_per_day_bars([])
        self.assertGreaterEqual(len(fig.layout.annotations), 1)
        # Empty throughput strip still pins the 150px thin-strip
        # height instead of collapsing back to Plotly's 450px default.
        self.assertEqual(fig.layout.height, 150)


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
                day=date(2026, 5, 1), event="agent_exit", count=1,
                cost_usd=1.0, input_tokens=10, output_tokens=10,
            ),
        ]
        fig = dashboard_charts.usage_over_time(points)
        self.assertEqual(fig.layout.height, 330)

    def test_horizontal_bars_height_scales_with_rows(self) -> None:
        # Three bars: ~40px per row + 80 = 200.
        items = [
            ("alpha", "1 run", 1.0, "#111"),
            ("beta", "2 runs", 2.0, "#222"),
            ("gamma", "3 runs", 3.0, "#333"),
        ]
        fig = dashboard_charts.cost_horizontal_bars(items)
        self.assertEqual(fig.layout.height, 40 * 3 + 80)

    def test_done_per_day_strip_height(self) -> None:
        rows = [
            ThroughputDayRow(day=date(2026, 5, 1), resolved=1, rejected=0),
        ]
        fig = dashboard_charts.done_per_day_bars(rows)
        # Throughput strip lives in the narrow reliability column;
        # 150px keeps it from dwarfing the tiles above it.
        self.assertEqual(fig.layout.height, 150)

    def test_heatmap_height_matches_mock_squares(self) -> None:
        # 7 rows x 24 columns: the standalone mock's compact square
        # cells need ~240px, not Plotly's default 450.
        fig = dashboard_charts.hour_weekday_heatmap([])
        self.assertEqual(fig.layout.height, 240)


if __name__ == "__main__":
    unittest.main()
