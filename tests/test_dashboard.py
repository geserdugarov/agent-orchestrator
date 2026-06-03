# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the non-Streamlit logic in `orchestrator.dashboard`.

The Streamlit, pandas, Plotly, and chart-builder imports inside
`dashboard.main` are deliberately lazy so the orchestrator polling
tick never pulls them in. These tests exercise the pure helpers
(date window math, preset window selection, KPI deltas, insight
banners, the disabled-DB banner, the issue-number drill-down
parser, and the cache-key shape) and assert the lazy-import
invariant -- the module must load even when `streamlit` is not on
the install path. That way the suite stays hermetic regardless of
which dependency group an operator synced.

The module-reload pattern mirrors `tests/test_analytics_read.py`:
re-import under a hermetic env so the dashboard's `from orchestrator
import analytics` picks up the patched `ANALYTICS_DB_URL` instead of
whatever the earlier test-session import left cached.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch


def _hermetic_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
    }
    if extra:
        env.update(extra)
    return env


def _reload(env: dict[str, str] | None = None):
    """Reload `analytics` + `dashboard` against the hermetic env.

    Import order matters: `analytics` must come back first so its
    fresh module object is installed as the
    `orchestrator.analytics` package attribute before `dashboard`'s
    `from orchestrator import analytics` runs -- otherwise
    `_handle_fromlist` returns the conftest-cached module and
    `dashboard.analytics.ANALYTICS_DB_URL` keeps the pre-patch value.
    `config` is popped too so the analytics package's
    `from .. import config` reloads against the patched env (it
    still reads `LOG_DIR` for the JSONL default).
    """
    with patch.dict(os.environ, _hermetic_env(env), clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.analytics.read", None)
        sys.modules.pop("orchestrator.analytics", None)
        sys.modules.pop("orchestrator.dashboard", None)
        import orchestrator.analytics as analytics
        import orchestrator.dashboard as dashboard
        return analytics, dashboard


class DefaultDateRangeTest(unittest.TestCase):

    def test_default_window_covers_n_days_including_today(self) -> None:
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=date(2026, 5, 28), days=7
        )
        self.assertEqual(end, date(2026, 5, 28))
        self.assertEqual(start, date(2026, 5, 22))

    def test_days_one_yields_today_only(self) -> None:
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=date(2026, 5, 28), days=1
        )
        self.assertEqual(start, end)

    def test_days_zero_clamps_to_today_only(self) -> None:
        # `days=0` is non-sensical (an empty window) so the helper
        # clamps to "today only" instead of returning end < start.
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=date(2026, 5, 28), days=0
        )
        self.assertEqual(start, date(2026, 5, 28))
        self.assertEqual(end, date(2026, 5, 28))


class ToWindowTest(unittest.TestCase):

    def test_inclusive_end_becomes_exclusive_midnight(self) -> None:
        # `analytics_read` uses `ts < end`; midnight on the day after
        # `end_date` is what makes events from `end_date` visible.
        _, dashboard = _reload()
        window = dashboard.to_window(date(2026, 5, 1), date(2026, 5, 3))
        self.assertEqual(
            window.start, datetime(2026, 5, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(
            window.end, datetime(2026, 5, 4, tzinfo=timezone.utc)
        )

    def test_reversed_range_is_swapped(self) -> None:
        # The Streamlit two-date input lets the user type end < start.
        # Swapping silently keeps the dashboard useful instead of
        # collapsing to an empty SQL window.
        _, dashboard = _reload()
        window = dashboard.to_window(date(2026, 5, 5), date(2026, 5, 1))
        self.assertEqual(window.start.date(), date(2026, 5, 1))
        self.assertEqual(window.end.date(), date(2026, 5, 6))

    def test_single_day_window(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(date(2026, 5, 1), date(2026, 5, 1))
        self.assertEqual(
            window.start, datetime(2026, 5, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(
            window.end, datetime(2026, 5, 2, tzinfo=timezone.utc)
        )


class ParseIssueNumberTest(unittest.TestCase):

    def test_bare_int(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.parse_issue_number("42"), 42)

    def test_hash_prefix_and_whitespace(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.parse_issue_number(" #42 "), 42)
        self.assertEqual(dashboard.parse_issue_number("# 42"), 42)

    def test_empty_returns_none(self) -> None:
        _, dashboard = _reload()
        self.assertIsNone(dashboard.parse_issue_number(""))
        self.assertIsNone(dashboard.parse_issue_number("   "))
        self.assertIsNone(dashboard.parse_issue_number("#"))

    def test_non_numeric_returns_none(self) -> None:
        _, dashboard = _reload()
        self.assertIsNone(dashboard.parse_issue_number("abc"))
        self.assertIsNone(dashboard.parse_issue_number("#abc"))

    def test_non_positive_returns_none(self) -> None:
        # GitHub issue numbers start at 1; 0 and negatives are not
        # valid drill-down targets.
        _, dashboard = _reload()
        self.assertIsNone(dashboard.parse_issue_number("0"))
        self.assertIsNone(dashboard.parse_issue_number("-3"))


class DbUnconfiguredMessageTest(unittest.TestCase):

    def test_unset_url_returns_message(self) -> None:
        _, dashboard = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            dashboard.db_unconfigured_message(),
            dashboard.UNCONFIGURED_DB_MESSAGE,
        )

    def test_disable_sentinel_returns_message(self) -> None:
        # Each of the documented disable sentinels collapses to None
        # inside `config`, so the helper should treat them the same.
        for sentinel in ("off", "disabled", "none", "OFF", "Disabled"):
            with self.subTest(sentinel=sentinel):
                _, dashboard = _reload({"ANALYTICS_DB_URL": sentinel})
                self.assertEqual(
                    dashboard.db_unconfigured_message(),
                    dashboard.UNCONFIGURED_DB_MESSAGE,
                )

    def test_configured_url_returns_none(self) -> None:
        _, dashboard = _reload(
            {"ANALYTICS_DB_URL": "postgresql://h/db"}
        )
        self.assertIsNone(dashboard.db_unconfigured_message())


class LazyImportTest(unittest.TestCase):
    """The dashboard module must load without importing `streamlit`
    or `plotly`.

    The polling tick loads `orchestrator.*` modules at process start;
    if `dashboard.py` were to import Streamlit (or Plotly via
    `dashboard_charts`) at module top, every orchestrator deployment
    would have to install the dashboard group. Lazy import inside
    `main()` is the boundary; this test is the guardrail.
    """

    def test_dashboard_only_modules_absent_after_load(self) -> None:
        with patch.dict(os.environ, _hermetic_env(), clear=True):
            sys.modules.pop("orchestrator.config", None)
            sys.modules.pop("orchestrator.analytics.read", None)
            sys.modules.pop("orchestrator.analytics", None)
            sys.modules.pop("orchestrator.dashboard", None)
            sys.modules.pop("orchestrator.dashboard_charts", None)
            sys.modules.pop("streamlit", None)
            sys.modules.pop("pandas", None)
            sys.modules.pop("plotly", None)
            import orchestrator.dashboard  # noqa: F401
            self.assertNotIn("streamlit", sys.modules)
            self.assertNotIn("pandas", sys.modules)
            self.assertNotIn("plotly", sys.modules)
            self.assertNotIn("orchestrator.dashboard_charts", sys.modules)


class ScriptPathLaunchTest(unittest.TestCase):
    """Guard the `streamlit run orchestrator/dashboard.py` launch path.

    The Streamlit launcher executes the file as a top-level script via
    `runpy` with no parent package and prepends the *script's*
    directory (not the repo root) to `sys.path`. A naked relative
    import (`from . import ...`) or a bare absolute import without a
    `sys.path` fix raises `ImportError: attempted relative import with
    no known parent package` before any Streamlit code can render --
    the reviewer caught exactly this regression with
    `AppTest.from_file(...).run()`. We reproduce that `sys.path` shape
    here instead of pulling Streamlit in (the dashboard dependency
    group is opt-in and not installed for the default test sync):
    strip the repo root, insert the script's dir, then `runpy` the
    file with a non-`__main__` run name so `main()` is not invoked.
    """

    def test_runs_without_repo_root_on_syspath(self) -> None:
        import runpy
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        dashboard_path = repo_root / "orchestrator" / "dashboard.py"
        script_dir = dashboard_path.parent

        original_path = list(sys.path)
        # Snapshot the `orchestrator.*` modules so a successful
        # re-import inside `runpy` does not poison the rest of the
        # test session with a half-initialised package.
        saved_modules = {
            k: v for k, v in sys.modules.items()
            if k == "orchestrator" or k.startswith("orchestrator.")
        }
        try:
            # Match Streamlit's launch shape: only the script's
            # directory is on sys.path, the repo root is not.
            resolved_root = repo_root.resolve()
            sys.path[:] = [
                p for p in sys.path
                if not p or Path(p).resolve() != resolved_root
            ]
            sys.path.insert(0, str(script_dir))
            for k in list(sys.modules):
                if k == "orchestrator" or k.startswith("orchestrator."):
                    del sys.modules[k]

            # `run_name="not_main"` keeps the `if __name__ == "__main__":`
            # block from firing, so the test does not require Streamlit
            # to be installed -- only the top-level imports must
            # succeed under the script-launch sys.path.
            namespace = runpy.run_path(
                str(dashboard_path), run_name="not_main"
            )
            self.assertIn("main", namespace)
            self.assertIn("analytics_read", namespace)
        finally:
            sys.path[:] = original_path
            for k in list(sys.modules):
                if k == "orchestrator" or k.startswith("orchestrator."):
                    del sys.modules[k]
            sys.modules.update(saved_modules)


class ResolveStageFilterTest(unittest.TestCase):
    """The stage multiselect default ('all known non-null stages')
    must collapse to `stages=None` so the read-model query does
    not emit a `stage IN (...)` clause that silently excludes
    NULL-stage rows. NULL stages are a legitimate case --
    `stage_evaluation` writes `stage=None` when the issue
    carries no workflow label. The cleared-multiselect signal
    (`[]`) must stay distinct so the reviewer-documented "show
    nothing" path still works.
    """

    def test_all_selected_collapses_to_none(self) -> None:
        _, dashboard = _reload()
        result = dashboard.resolve_stage_filter(
            selected=["implementing", "validating"],
            available=("implementing", "validating"),
        )
        self.assertIsNone(result)

    def test_no_available_options_returns_none(self) -> None:
        # Empty filter options (DB is empty or has no non-null
        # stages yet) collapses to `None` so the read-model query
        # runs unconstrained on the stage column.
        _, dashboard = _reload()
        result = dashboard.resolve_stage_filter(
            selected=[], available=()
        )
        self.assertIsNone(result)

    def test_cleared_multiselect_returns_empty_list(self) -> None:
        # Options exist but the operator cleared the selection.
        # The read model encodes `[]` as a tautologically-false
        # predicate; without this branch the cleared state would
        # be indistinguishable from the all-selected default.
        _, dashboard = _reload()
        result = dashboard.resolve_stage_filter(
            selected=[],
            available=("implementing", "validating"),
        )
        self.assertEqual(result, [])

    def test_proper_subset_passes_through(self) -> None:
        _, dashboard = _reload()
        result = dashboard.resolve_stage_filter(
            selected=["implementing"],
            available=("implementing", "validating"),
        )
        self.assertEqual(result, ["implementing"])


class PresetWindowTest(unittest.TestCase):
    """The data-extent-bounded presets anchor at the data extent's
    max date (not today): a freshly-deployed Postgres whose latest
    event is a few days old should still surface a useful window
    without the operator having to flip to Custom and reach for a
    calendar. The redesigned page exposes `3D` / `7D` / `All` inline
    in the topbar; `Custom` stays available as the sidebar fallback.
    """

    def _extent(self, min_d, max_d):
        _, dashboard = _reload()
        return dashboard.DataExtent(
            min_ts=datetime(min_d.year, min_d.month, min_d.day,
                            tzinfo=timezone.utc),
            max_ts=datetime(max_d.year, max_d.month, max_d.day, 23, 59,
                            tzinfo=timezone.utc),
        )

    def test_three_day_preset_anchors_at_max(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(date(2026, 5, 1), date(2026, 5, 28))
        window = dashboard.preset_window(dashboard.PRESET_3D, extent)
        self.assertIsNotNone(window)
        # Three-day preset spans the max date and the two days before
        # it, exclusive end at midnight the day after the max.
        self.assertEqual(window.start.date(), date(2026, 5, 26))
        self.assertEqual(window.end.date(), date(2026, 5, 29))

    def test_seven_day_preset_anchors_at_max(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(date(2026, 5, 1), date(2026, 5, 28))
        window = dashboard.preset_window(dashboard.PRESET_7D, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), date(2026, 5, 22))
        self.assertEqual(window.end.date(), date(2026, 5, 29))

    def test_seven_day_preset_clamps_to_min(self) -> None:
        # Data extent is only 3 days wide -- "Last 7 days" must
        # clamp the start at the data extent's min, not reach
        # before it.
        _, dashboard = _reload()
        extent = self._extent(date(2026, 5, 26), date(2026, 5, 28))
        window = dashboard.preset_window(dashboard.PRESET_7D, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), date(2026, 5, 26))
        self.assertEqual(window.end.date(), date(2026, 5, 29))

    def test_all_preset_covers_full_extent(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(date(2026, 1, 1), date(2026, 5, 28))
        window = dashboard.preset_window(dashboard.PRESET_ALL, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), date(2026, 1, 1))
        self.assertEqual(window.end.date(), date(2026, 5, 29))

    def test_custom_preset_returns_none(self) -> None:
        # The caller renders a date-range picker when the preset is
        # `Custom`; `preset_window` returns `None` so the caller can
        # branch on a falsy value rather than special-casing the
        # preset string in two places.
        _, dashboard = _reload()
        extent = self._extent(date(2026, 5, 1), date(2026, 5, 28))
        self.assertIsNone(
            dashboard.preset_window(dashboard.PRESET_CUSTOM, extent)
        )

    def test_empty_extent_returns_none(self) -> None:
        _, dashboard = _reload()
        empty = dashboard.DataExtent()
        self.assertIsNone(
            dashboard.preset_window(dashboard.PRESET_7D, empty)
        )

    def test_unknown_preset_returns_none(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(date(2026, 5, 1), date(2026, 5, 28))
        self.assertIsNone(
            dashboard.preset_window("not-a-preset", extent)
        )

    def test_preset_options_match_redesign(self) -> None:
        # Pin the inline labels the topbar exposes (3D / 7D / All)
        # and the full option tuple including the Custom fallback so
        # a future refactor cannot silently re-introduce the old
        # `30d` preset.
        _, dashboard = _reload()
        self.assertEqual(
            dashboard.PRESET_OPTIONS,
            (dashboard.PRESET_3D, dashboard.PRESET_7D,
             dashboard.PRESET_ALL, dashboard.PRESET_CUSTOM),
        )
        self.assertEqual(
            set(dashboard.PRESET_INLINE_LABELS),
            {dashboard.PRESET_3D, dashboard.PRESET_7D,
             dashboard.PRESET_ALL},
        )


class PreviousWindowTest(unittest.TestCase):
    """The previous-window helper feeds the KPI delta column. It must
    return a window of the same length immediately before `window`
    so the deltas compare like-for-like (e.g. last-30-days vs the
    30 days before that).
    """

    def test_length_preserved(self) -> None:
        _, dashboard = _reload()
        win = dashboard.to_window(date(2026, 5, 1), date(2026, 5, 7))
        prev = dashboard.previous_window(win)
        self.assertEqual(prev.end, win.start)
        self.assertEqual(prev.end - prev.start, win.end - win.start)

    def test_seven_day_window_yields_seven_day_previous(self) -> None:
        _, dashboard = _reload()
        win = dashboard.to_window(date(2026, 5, 22), date(2026, 5, 28))
        prev = dashboard.previous_window(win)
        # `to_window`'s end is exclusive (one day past `end_date`),
        # so the seven-day window spans 7 calendar days; the previous
        # window starts seven days before the current start.
        self.assertEqual(prev.start.date(), date(2026, 5, 15))
        self.assertEqual(prev.end.date(), date(2026, 5, 22))


class KpiDeltaTest(unittest.TestCase):

    def test_positive_delta(self) -> None:
        _, dashboard = _reload()
        self.assertAlmostEqual(dashboard.kpi_delta(125, 100), 0.25)

    def test_negative_delta(self) -> None:
        _, dashboard = _reload()
        self.assertAlmostEqual(dashboard.kpi_delta(75, 100), -0.25)

    def test_zero_previous_returns_none(self) -> None:
        # The dashboard hides the delta indicator rather than
        # rendering an infinity for the zero-baseline case.
        _, dashboard = _reload()
        self.assertIsNone(dashboard.kpi_delta(10, 0))

    def test_negative_previous_returns_none(self) -> None:
        _, dashboard = _reload()
        self.assertIsNone(dashboard.kpi_delta(10, -5))


class ComputeInsightsTest(unittest.TestCase):
    """The insight banners are derived computationally from the
    read-model rows; this test pins the threshold semantics so a
    future tuning pass changes them deliberately.
    """

    def _summary(
        self,
        *,
        events=0,
        cost=0.0,
        agent_runs=0,
        failed=0,
    ):
        _, dashboard = _reload()
        return dashboard.Summary(
            total_events=events,
            total_agent_runs=agent_runs,
            failed_agent_runs=failed,
            total_cost_usd=cost,
        )

    def test_no_banners_for_healthy_window(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(events=100, agent_runs=50, failed=0, cost=10.0)
        prev = self._summary(events=90, agent_runs=45, failed=0, cost=9.0)
        self.assertEqual(
            dashboard.compute_insights(summary, prev_summary=prev),
            [],
        )

    def test_high_failure_rate_emits_error(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(agent_runs=10, failed=3)
        banners = dashboard.compute_insights(summary)
        self.assertEqual(len(banners), 1)
        self.assertEqual(banners[0].severity, "error")
        self.assertIn("3 of 10", banners[0].message)

    def test_low_failure_rate_skips_banner(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(agent_runs=100, failed=5)
        self.assertEqual(dashboard.compute_insights(summary), [])

    def test_cost_surge_emits_warning(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(cost=200.0)
        prev = self._summary(cost=100.0)
        banners = dashboard.compute_insights(summary, prev_summary=prev)
        # Cost surge is the only banner here -- previous failures are
        # zero so the failure-rate banner does not fire.
        self.assertTrue(
            any(
                b.severity == "warning" and "up 100%" in b.message
                for b in banners
            )
        )

    def test_cost_drop_emits_info(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(cost=50.0)
        prev = self._summary(cost=100.0)
        banners = dashboard.compute_insights(summary, prev_summary=prev)
        self.assertTrue(
            any(
                b.severity == "info" and "down 50%" in b.message
                for b in banners
            )
        )

    def test_unpriced_coverage_emits_warning(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import CostCoverageRow
        summary = self._summary()
        cov = [
            CostCoverageRow(cost_source="reported", runs=70),
            CostCoverageRow(cost_source="unknown-price", runs=20),
            CostCoverageRow(cost_source="unknown", runs=10),
        ]
        banners = dashboard.compute_insights(
            summary, cost_coverage_rows=cov
        )
        # 30 / 100 = 30% unpriced -- well over the 10% threshold.
        self.assertTrue(
            any(
                b.severity == "warning"
                and "30 of 100" in b.message
                for b in banners
            )
        )

    def test_unpriced_below_threshold_skips(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import CostCoverageRow
        summary = self._summary()
        cov = [
            CostCoverageRow(cost_source="reported", runs=99),
            CostCoverageRow(cost_source="unknown-price", runs=1),
        ]
        self.assertEqual(
            dashboard.compute_insights(summary, cost_coverage_rows=cov),
            [],
        )

    def test_rework_share_above_threshold_emits_warning(self) -> None:
        # Spend in review rounds >= 1 exceeds 30 % -- surface the
        # "rework dominates" insight from the standalone mock.
        _, dashboard = _reload()
        from orchestrator.analytics.read import ReviewRoundBucketRow
        summary = self._summary()
        rounds = [
            ReviewRoundBucketRow(
                bucket="0", runs=10, failed=0, total_cost_usd=40.0
            ),
            ReviewRoundBucketRow(
                bucket="1", runs=5, failed=0, total_cost_usd=30.0
            ),
            ReviewRoundBucketRow(
                bucket="3-5", runs=2, failed=1, total_cost_usd=30.0
            ),
        ]
        banners = dashboard.compute_insights(
            summary, review_round_rows=rounds
        )
        self.assertTrue(
            any(
                b.severity == "warning"
                and "Rework dominates spend" in b.message
                for b in banners
            )
        )

    def test_rework_share_below_threshold_skips(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import ReviewRoundBucketRow
        summary = self._summary()
        rounds = [
            ReviewRoundBucketRow(
                bucket="0", runs=10, failed=0, total_cost_usd=80.0
            ),
            ReviewRoundBucketRow(
                bucket="1", runs=1, failed=0, total_cost_usd=10.0
            ),
        ]
        self.assertEqual(
            dashboard.compute_insights(summary, review_round_rows=rounds),
            [],
        )

    def test_back_loaded_spend_emits_info(self) -> None:
        # validating + documenting > implementing while implementing
        # is non-zero -- surface the standalone mock's "Spend is
        # back-loaded" insight.
        _, dashboard = _reload()
        from orchestrator.analytics.read import StageBreakdown
        summary = self._summary()
        stages = [
            StageBreakdown(
                stage="implementing", count=10, total_cost_usd=20.0,
                runs=5,
            ),
            StageBreakdown(
                stage="validating", count=4, total_cost_usd=15.0,
                runs=4,
            ),
            StageBreakdown(
                stage="documenting", count=3, total_cost_usd=10.0,
                runs=3,
            ),
        ]
        banners = dashboard.compute_insights(
            summary, stage_rows=stages
        )
        self.assertTrue(
            any(
                b.severity == "info"
                and "Spend is back-loaded" in b.message
                and "implementing ($20.00)" in b.message
                for b in banners
            )
        )

    def test_back_loaded_skipped_when_implementing_dominates(self) -> None:
        # validating + documenting <= implementing -- no banner.
        _, dashboard = _reload()
        from orchestrator.analytics.read import StageBreakdown
        summary = self._summary()
        stages = [
            StageBreakdown(
                stage="implementing", count=20, total_cost_usd=80.0,
                runs=10,
            ),
            StageBreakdown(
                stage="validating", count=2, total_cost_usd=8.0,
                runs=2,
            ),
            StageBreakdown(
                stage="documenting", count=1, total_cost_usd=4.0,
                runs=1,
            ),
        ]
        self.assertEqual(
            dashboard.compute_insights(summary, stage_rows=stages),
            [],
        )

    def test_back_loaded_skipped_when_implementing_zero(self) -> None:
        # When implementing cost is zero the ratio is meaningless;
        # the standalone mock guards on `iCost > 0` and we mirror
        # that.
        _, dashboard = _reload()
        from orchestrator.analytics.read import StageBreakdown
        summary = self._summary()
        stages = [
            StageBreakdown(
                stage="validating", count=2, total_cost_usd=8.0,
                runs=2,
            ),
        ]
        self.assertEqual(
            dashboard.compute_insights(summary, stage_rows=stages),
            [],
        )


class ReliabilityTileDataTest(unittest.TestCase):
    """The redesigned reliability panel sources every tile from
    `Summary`'s window-wide aggregates so a long window with more
    than `DEFAULT_RECENT_AGENT_EXITS` (100) rows still sees every
    timeout / failure -- the earlier draft computed these off the
    LIMIT-capped recent-runs read and silently undercounted."""

    def _summary(self, **kw):
        _, dashboard = _reload()
        from orchestrator.analytics.read import Summary
        return Summary(**kw)

    def test_timeouts_sourced_from_summary_full_window(self) -> None:
        _, dashboard = _reload()
        # Window holds 250 agent runs (far more than the 100-row
        # recent-runs cap) with 17 timeouts and 4 failures.
        summary = self._summary(
            total_agent_runs=250,
            failed_agent_runs=4,
            timed_out_agent_runs=17,
        )
        tiles = dashboard.reliability_tile_data(
            summary, resolved=12, rejected=2,
        )
        by_label = {lbl: (val, tone) for val, lbl, tone in tiles}
        # Headline tiles all pulled off Summary directly:
        self.assertEqual(by_label["Agent runs"][0], 250)
        self.assertEqual(by_label["Failures"][0], 4)
        self.assertEqual(by_label["Timeouts"][0], 17)
        # Tone flips when the count crosses zero so the CSS class
        # paints the tile.
        self.assertEqual(by_label["Timeouts"][1], "bad")
        self.assertEqual(by_label["Failures"][1], "warn")

    def test_zero_runs_does_not_divide_by_zero(self) -> None:
        # Empty window: success rate collapses to 0% (no runs, no
        # successes) instead of raising a ZeroDivisionError. The
        # redesigned page renders the tile anyway so the operator
        # can confirm the window really is empty.
        _, dashboard = _reload()
        summary = self._summary(
            total_agent_runs=0,
            failed_agent_runs=0,
            timed_out_agent_runs=0,
        )
        tiles = dashboard.reliability_tile_data(summary)
        by_label = {lbl: val for val, lbl, _ in tiles}
        self.assertEqual(by_label["Agent runs"], 0)
        self.assertEqual(by_label["Success rate"], "0%")
        self.assertEqual(by_label["Timeouts"], 0)

    def test_clean_window_has_neutral_tones(self) -> None:
        # No failures, no timeouts: the warn / bad tones drop off
        # so the panel reads as healthy at a glance.
        _, dashboard = _reload()
        summary = self._summary(
            total_agent_runs=20,
            failed_agent_runs=0,
            timed_out_agent_runs=0,
        )
        tiles = dashboard.reliability_tile_data(summary)
        by_label = {lbl: tone for _, lbl, tone in tiles}
        self.assertEqual(by_label["Failures"], "")
        self.assertEqual(by_label["Timeouts"], "")


class ReworkTotalsTest(unittest.TestCase):
    """The rework KPI tile and the "rework dominates" insight both
    read off `rework_totals`. Pin the shape so a future tweak does
    not silently shift which buckets count as rework.
    """

    def test_initial_bucket_excluded(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import ReviewRoundBucketRow
        rows = [
            ReviewRoundBucketRow(
                bucket="0", runs=5, failed=0, total_cost_usd=50.0
            ),
            ReviewRoundBucketRow(
                bucket="1", runs=2, failed=1, total_cost_usd=20.0
            ),
        ]
        total, rework = dashboard.rework_totals(rows)
        self.assertAlmostEqual(total, 70.0)
        self.assertAlmostEqual(rework, 20.0)

    def test_unknown_bucket_excluded(self) -> None:
        # `unknown` is pre-review work surfaced for visibility, NOT
        # rework -- exclude it from the rework cost.
        _, dashboard = _reload()
        from orchestrator.analytics.read import ReviewRoundBucketRow
        rows = [
            ReviewRoundBucketRow(
                bucket="unknown", runs=3, failed=0, total_cost_usd=10.0
            ),
            ReviewRoundBucketRow(
                bucket="2", runs=1, failed=0, total_cost_usd=5.0
            ),
        ]
        total, rework = dashboard.rework_totals(rows)
        self.assertAlmostEqual(total, 15.0)
        self.assertAlmostEqual(rework, 5.0)

    def test_empty_rows_returns_zero(self) -> None:
        _, dashboard = _reload()
        total, rework = dashboard.rework_totals([])
        self.assertEqual((total, rework), (0.0, 0.0))


class TopExpensiveIssuesTest(unittest.TestCase):

    def _issue(self, repo, num, cost, events=1):
        _, dashboard = _reload()
        from orchestrator.analytics.read import IssueSummaryRow
        return IssueSummaryRow(
            repo=repo,
            issue=num,
            event_count=events,
            first_seen=datetime(2026, 5, 1, tzinfo=timezone.utc),
            last_seen=datetime(2026, 5, 2, tzinfo=timezone.utc),
            latest_stage="implementing",
            agent_exits=1,
            total_cost_usd=cost,
            total_input_tokens=0,
            total_output_tokens=0,
        )

    def test_sorts_by_cost_desc(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue("acme/a", 1, 0.10),
            self._issue("acme/b", 2, 1.00),
            self._issue("acme/c", 3, 0.50),
        ]
        top = dashboard.top_expensive_issues(rows, limit=2)
        self.assertEqual([(r.repo, r.issue) for r in top],
                         [("acme/b", 2), ("acme/c", 3)])

    def test_none_cost_sorts_last(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue("acme/a", 1, None),
            self._issue("acme/b", 2, 0.10),
        ]
        top = dashboard.top_expensive_issues(rows, limit=5)
        self.assertEqual([r.issue for r in top], [2, 1])

    def test_limit_zero_returns_empty(self) -> None:
        _, dashboard = _reload()
        rows = [self._issue("acme/a", 1, 0.10)]
        self.assertEqual(dashboard.top_expensive_issues(rows, limit=0), [])

    def test_ties_break_on_event_count_then_identity(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue("acme/a", 1, 1.00, events=2),
            self._issue("acme/a", 2, 1.00, events=10),
            self._issue("acme/b", 1, 1.00, events=2),
        ]
        top = dashboard.top_expensive_issues(rows)
        # Higher event count first, then (repo, issue) ascending.
        self.assertEqual(
            [(r.repo, r.issue) for r in top],
            [("acme/a", 2), ("acme/a", 1), ("acme/b", 1)],
        )


class IssuesTableHtmlTest(unittest.TestCase):
    """The "Most expensive issues" panel is hand-rolled HTML (rather
    than `st.dataframe`) so it can carry the standalone mock's
    in-row cost bars and clean / fail status pills.
    """

    def _row(self, repo, issue, cost, *, failed=0, max_round=None,
             max_retry=None):
        _, dashboard = _reload()
        from datetime import datetime, timezone
        from orchestrator.analytics.read import IssueSummaryRow
        return IssueSummaryRow(
            repo=repo,
            issue=issue,
            event_count=10,
            first_seen=datetime(2026, 5, 1, tzinfo=timezone.utc),
            last_seen=datetime(2026, 5, 2, tzinfo=timezone.utc),
            latest_stage="implementing",
            agent_exits=4,
            total_cost_usd=cost,
            total_input_tokens=0,
            total_output_tokens=0,
            max_review_round=max_round,
            failed_agent_runs=failed,
            max_retry_count=max_retry,
        )

    def test_columns_match_standalone_mock(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("acme/a", 1, 12.0)]
        html_out = dashboard._issues_table_html(rows)
        for header in ("Issue", "Cost", "Runs", "Review rds",
                       "Retries", "Status"):
            self.assertIn(f">{header}<", html_out)

    def test_status_pill_renders_clean_when_no_failures(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("acme/a", 1, 4.0, failed=0)]
        html_out = dashboard._issues_table_html(rows)
        self.assertIn('class="orch-pill ok"', html_out)
        self.assertIn(">clean<", html_out)
        self.assertNotIn('class="orch-pill bad"', html_out)

    def test_status_pill_renders_fail_when_failures_present(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("acme/a", 1, 4.0, failed=3)]
        html_out = dashboard._issues_table_html(rows)
        self.assertIn('class="orch-pill bad"', html_out)
        self.assertIn(">3 fail<", html_out)

    def test_in_row_cost_bar_relative_to_max(self) -> None:
        # Cheapest issue's bar is a fraction of the most expensive
        # issue's full-width bar.
        _, dashboard = _reload()
        rows = [
            self._row("acme/a", 1, 10.0),
            self._row("acme/b", 2, 5.0),
        ]
        html_out = dashboard._issues_table_html(rows)
        # Full-width bar on the most expensive issue and a half-
        # width bar on the cheaper one.
        self.assertIn("width:100.0%", html_out)
        self.assertIn("width:50.0%", html_out)

    def test_review_rounds_three_or_more_warn_tone(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("acme/a", 1, 4.0, max_round=4)]
        html_out = dashboard._issues_table_html(rows)
        # High-review-round cells get the warn class so the operator
        # can spot rework-heavy issues at a glance.
        self.assertIn('class="orch-badge-warn">4', html_out)


class CacheKeyTest(unittest.TestCase):
    """`st.cache_data` hashes the cache key tuple; lists from
    multiselects need to become tuples, and `None` must be preserved
    so the tri-state filter contract (None / [] / [...]) does not
    collapse at the cache layer.
    """

    def test_lists_become_tuples(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(date(2026, 5, 1), date(2026, 5, 7))
        key = dashboard.cache_key(
            window, "acme/widgets",
            ["agent_exit", "stage_enter"], ["implementing"], 42,
        )
        self.assertEqual(
            key,
            (
                window.start,
                window.end,
                "acme/widgets",
                ("agent_exit", "stage_enter"),
                ("implementing",),
                42,
            ),
        )
        hash(key)  # must be hashable

    def test_none_is_preserved(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(date(2026, 5, 1), date(2026, 5, 7))
        key = dashboard.cache_key(window, None, None, None, None)
        self.assertEqual(
            key, (window.start, window.end, None, None, None, None)
        )

    def test_empty_list_distinct_from_none(self) -> None:
        # Empty events / stages mean "cleared multiselect, show
        # nothing"; the cache key must keep the empty tuple distinct
        # from None so the two SQL shapes do not collide in cache.
        _, dashboard = _reload()
        window = dashboard.to_window(date(2026, 5, 1), date(2026, 5, 7))
        empty = dashboard.cache_key(window, "r", [], [], None)
        none = dashboard.cache_key(window, "r", None, None, None)
        self.assertNotEqual(empty, none)
        self.assertEqual(empty[3], ())
        self.assertEqual(empty[4], ())


if __name__ == "__main__":
    unittest.main()
