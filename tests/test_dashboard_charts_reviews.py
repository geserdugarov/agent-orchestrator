# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard review-round and repository cost chart tests."""

import importlib


import unittest

_USE_LOGICAL_ORDER_RUNS = 12
_USE_LOGICAL_ORDER_TOTAL_COST = 40.0
_USE_LOGICAL_ORDER_DEVELOPER = 28.0
_USE_LOGICAL_ORDER_REVIEWER_C = 12.0
_USE_LOGICAL_ORDER_DE_SECONDARY = 20.0
_USE_LOGICAL_ORDER_DEV_TERTIARY = 8.0
_USE_LOGICAL_ORDER_RE_SECONDARY = 9.0
_USE_LOGICAL_ORDER_REVIEWER_N = 3.0
_USE_LOGICAL_ORDER_TO_SECONDARY = 20.0
_USE_LOGICAL_ORDER_D_QUATERNARY = 9.0
_USE_LOGICAL_ORDER_REV_TERTIARY = 11.0
_USE_LOGICAL_ORDER_DEVE_QUINARY = 7.0
_USE_LOGICAL_ORDER_DEVEL_SENARY = 2.0
_USE_LOGICAL_ORDER_R_QUATERNARY = 8.0
_USE_LOGICAL_ORDER_TOT_TERTIARY = 15.0
_USE_LOGICAL_ORDER_DE_SEPTENARY = 6.0
_USE_LOGICAL_ORDER_T_QUATERNARY = 5.0
_USE_LOGICAL_ORDER_DEV_OCTONARY = 5.0
_LIGHTER_ROLE_COLOR_TOTAL_COS = 20.0
_LIGHTER_ROLE_COLOR_DEVELOPER = 10.0
_LIGHTER_ROLE_COLOR_REVIEWER = 10.0
_LIGHTER_ROLE_COLOR_D_SECONDARY = 6.0
_LIGHTER_ROLE_COLOR_DE_TERTIARY = 4.0
_LIGHTER_ROLE_COLOR_R_SECONDARY = 7.0
_LIGHTER_ROLE_COLOR_RE_TERTIARY = 3.0
_OWNER_PREFIX_LEGIBILITY_TOTA = 8.0
_OWNER_PREFIX_LEGIBIL_SECONDARY = 3.0


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


ROLE_DEVELOPER = "developer"


ROLE_REVIEWER = "reviewer"


RGBA_PREFIX = "rgba("


EXPECTED_RGBA_MESSAGE = "expected rgba() cache shade, got "


TWO_RUNS_LABEL = "2 runs"


_PLACEHOLDER_HEIGHT = 120


@unittest.skipUnless(HAS_PLOTLY, _SKIP_REASON)
class CostByReviewRoundTest(unittest.TestCase):
    def test_round_labels_use_logical_order(self) -> None:
        rows = [
            ReviewRoundBucketRow(
                bucket="0",
                runs=_USE_LOGICAL_ORDER_RUNS,
                failed=0,
                total_cost_usd=_USE_LOGICAL_ORDER_TOTAL_COST,
                developer_runs=7,
                reviewer_runs=5,
                developer_cost_usd=_USE_LOGICAL_ORDER_DEVELOPER,
                reviewer_cost_usd=_USE_LOGICAL_ORDER_REVIEWER_C,
                developer_cache_cost_usd=_USE_LOGICAL_ORDER_DE_SECONDARY,
                developer_no_cache_cost_usd=_USE_LOGICAL_ORDER_DEV_TERTIARY,
                reviewer_cache_cost_usd=_USE_LOGICAL_ORDER_RE_SECONDARY,
                reviewer_no_cache_cost_usd=_USE_LOGICAL_ORDER_REVIEWER_N,
            ),
            ReviewRoundBucketRow(
                bucket="1",
                runs=4,
                failed=1,
                total_cost_usd=_USE_LOGICAL_ORDER_TO_SECONDARY,
                developer_runs=2,
                reviewer_runs=2,
                developer_cost_usd=_USE_LOGICAL_ORDER_D_QUATERNARY,
                reviewer_cost_usd=_USE_LOGICAL_ORDER_REV_TERTIARY,
                developer_cache_cost_usd=_USE_LOGICAL_ORDER_DEVE_QUINARY,
                developer_no_cache_cost_usd=_USE_LOGICAL_ORDER_DEVEL_SENARY,
                reviewer_cache_cost_usd=_USE_LOGICAL_ORDER_R_QUATERNARY,
                reviewer_no_cache_cost_usd=_USE_LOGICAL_ORDER_REVIEWER_N,
            ),
            ReviewRoundBucketRow(
                bucket="3",
                runs=2,
                failed=2,
                total_cost_usd=_USE_LOGICAL_ORDER_TOT_TERTIARY,
                developer_runs=1,
                reviewer_runs=1,
                developer_cost_usd=_USE_LOGICAL_ORDER_DE_SEPTENARY,
                reviewer_cost_usd=_USE_LOGICAL_ORDER_RE_SECONDARY,
                developer_cache_cost_usd=_USE_LOGICAL_ORDER_DE_SEPTENARY,
                developer_no_cache_cost_usd=float(),
                reviewer_cache_cost_usd=_USE_LOGICAL_ORDER_RE_SECONDARY,
                reviewer_no_cache_cost_usd=float(),
            ),
            ReviewRoundBucketRow(
                bucket="unknown",
                runs=1,
                failed=0,
                total_cost_usd=_USE_LOGICAL_ORDER_T_QUATERNARY,
                developer_runs=1,
                reviewer_runs=0,
                developer_cost_usd=_USE_LOGICAL_ORDER_DEV_OCTONARY,
                reviewer_cost_usd=float(),
                developer_cache_cost_usd=float(),
                developer_no_cache_cost_usd=_USE_LOGICAL_ORDER_DEV_OCTONARY,
                reviewer_cache_cost_usd=float(),
                reviewer_no_cache_cost_usd=float(),
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
                bucket="0",
                runs=4,
                failed=0,
                total_cost_usd=_LIGHTER_ROLE_COLOR_TOTAL_COS,
                developer_runs=2,
                reviewer_runs=2,
                developer_cost_usd=_LIGHTER_ROLE_COLOR_DEVELOPER,
                reviewer_cost_usd=_LIGHTER_ROLE_COLOR_REVIEWER,
                developer_cache_cost_usd=_LIGHTER_ROLE_COLOR_D_SECONDARY,
                developer_no_cache_cost_usd=_LIGHTER_ROLE_COLOR_DE_TERTIARY,
                reviewer_cache_cost_usd=_LIGHTER_ROLE_COLOR_R_SECONDARY,
                reviewer_no_cache_cost_usd=_LIGHTER_ROLE_COLOR_RE_TERTIARY,
            ),
        ]
        fig = dashboard_charts.cost_by_review_round(rows)
        role_colors = [trace.marker.color for trace in fig.data]
        # The no-cache base traces (indices 0 and 2) use the canonical
        # reviewer / developer role color verbatim.
        self.assertEqual(
            role_colors[0],
            theme.AGENT_ROLE_COLORS[ROLE_REVIEWER],
        )
        self.assertEqual(
            role_colors[2],
            theme.AGENT_ROLE_COLORS[ROLE_DEVELOPER],
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
            ROLE_REVIEWER,
            ROLE_REVIEWER,
            ROLE_DEVELOPER,
            ROLE_DEVELOPER,
        ]
        for trace, offsetgroup in zip(traces, expected_offsetgroups):
            self.assertEqual(trace.offsetgroup, offsetgroup)
            self.assertEqual(len(trace.y), 4)
        self.assertEqual(layout.legend.traceorder, "reversed")

    def _assert_round_labels(self, y_labels) -> None:
        joined = " ".join(y_labels)
        for needle in (
            "Initial",
            "Round 1",
            "Round 3",
            "No review round",
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
            [float(), float(), 3.0, 3.0],
            [float(), 9.0, 8.0, 9.0],
            [5.0, float(), 2.0, 8.0],
            [float(), 6.0, 7.0, 20.0],
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
                repo="acme/widgets",
                issues=2,
                events=10,
                agent_exits=4,
                total_cost_usd=_OWNER_PREFIX_LEGIBILITY_TOTA,
            ),
            RepoBreakdownRow(
                repo="acme/gadgets",
                issues=1,
                events=4,
                agent_exits=2,
                total_cost_usd=_OWNER_PREFIX_LEGIBIL_SECONDARY,
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
