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


# Hermetic reload env: skip .env autoloading and point the token file at a
# guaranteed-missing path so no ambient GITHUB_TOKEN leaks into a test.
SKIP_DOTENV_ENV = "ORCHESTRATOR_SKIP_DOTENV"
TOKEN_FILE_ENV = "ORCHESTRATOR_TOKEN_FILE"
MISSING_TOKEN_FILE = "/tmp/agent-orchestrator-token-missing"


def _hermetic_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        SKIP_DOTENV_ENV: "1",
        TOKEN_FILE_ENV: MISSING_TOKEN_FILE,
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

    The extracted helper modules (`dashboard_state` / `dashboard_kpis`
    / `dashboard_html`) are popped alongside `dashboard` so the
    re-imported facade re-binds them too -- otherwise a cached
    `dashboard_state` would keep its pre-patch `from orchestrator
    import analytics` reference and its module-import parse of
    `DASHBOARD_PARALLEL_READS`, defeating the hermetic reload.
    """
    with patch.dict(os.environ, _hermetic_env(env), clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.analytics.read", None)
        sys.modules.pop("orchestrator.analytics", None)
        sys.modules.pop("orchestrator.dashboard_state", None)
        sys.modules.pop("orchestrator.dashboard_kpis", None)
        sys.modules.pop("orchestrator.dashboard_html", None)
        sys.modules.pop("orchestrator.dashboard", None)
        import orchestrator.analytics as analytics
        import orchestrator.dashboard as dashboard
        return analytics, dashboard


# The dashboard's only configuration input is the analytics DB URL env
# var; tests flip it between "unset" (the disabled-DB banner / hermetic
# reload) and a syntactically-valid Postgres URL (the source-inspection
# reloads that read `dashboard.main`).
ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"
PARALLEL_READS_ENV = "DASHBOARD_PARALLEL_READS"
CONFIGURED_DB_URL = "postgresql://h/db"

# Recurring May-2026 anchors for the window / preset / KPI-delta tests.
# The canonical current window is MAY_22..MAY_28 (7 days); the preset
# data extent spans MAY_1..MAY_28 with its exclusive end at MAY_29, and
# the 3-day preset opens at MAY_26.
MAY_1 = date(2026, 5, 1)
MAY_7 = date(2026, 5, 7)
MAY_22 = date(2026, 5, 22)
MAY_26 = date(2026, 5, 26)
MAY_28 = date(2026, 5, 28)
MAY_29 = date(2026, 5, 29)

# Incidental first/last-seen timestamps stamped by the issue-summary row
# builders. Never asserted -- the builders only need a valid ordered pair.
FIRST_SEEN = datetime(2026, 5, 1, tzinfo=timezone.utc)
LAST_SEEN = datetime(2026, 5, 2, tzinfo=timezone.utc)

# Sample repo slugs shared by the issue-summary builders.
REPO_A = "acme/a"
REPO_B = "acme/b"
REPO_C = "acme/c"

# Fan-out reader names grouped by staged-render wave (issue #379): the
# first wave feeds the topbar / KPI strip, the second the remaining
# widgets.
FIRST_WAVE_READER_NAMES = (
    "summary", "prev_summary", "ts_points",
    "review_round_rows", "throughput_rows", "cost_coverage_rows",
)
SECOND_WAVE_READER_NAMES = (
    "stage_rows", "agent_exits", "issues_rows",
    "backend_rows", "repo_rows", "heatmap_rows",
    "backend_daily_rows", "skill_rows", "skill_matrix_rows",
)

# Canonical drill-down issue number, shared by the parse + cache-key tests.
ISSUE_NUMBER = 42

# Cache-key fixture inputs: a sample repo plus the event / stage filter
# selections whose list->tuple normalization the cache key must preserve.
CACHE_REPO = "acme/widgets"
EVENT_NAMES = ("agent_exit", "stage_enter")
STAGE_NAMES = ("implementing",)

# Skill-matrix sort contract: the query-param names the clickable headers
# write, the two direction tokens, and the column keys in header order.
MTX_SORT_PARAM = "mtx_sort"
MTX_DIR_PARAM = "mtx_dir"
SORT_ASC = "asc"
SORT_DESC = "desc"
MTX_SORT_KEYS = (
    "repo", "role", "backend", "skill", "runs", "skill_runs", "rate",
)


class _MainSourceTest(unittest.TestCase):
    """Base for tests that inspect `dashboard.main`'s source under a
    configured DB URL.

    Streamlit / Plotly are opt-in (not installed for the default
    `uv sync --locked`), so these read the rendered function source
    rather than driving the page under Streamlit.
    """

    def _main_source(self) -> str:
        import inspect
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})
        return inspect.getsource(dashboard.main)


class DefaultDateRangeTest(unittest.TestCase):

    def test_default_window_covers_n_days_including_today(self) -> None:
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=MAY_28, days=7
        )
        self.assertEqual(end, MAY_28)
        self.assertEqual(start, MAY_22)

    def test_days_one_yields_today_only(self) -> None:
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=MAY_28, days=1
        )
        self.assertEqual(start, end)

    def test_days_zero_clamps_to_today_only(self) -> None:
        # `days=0` is non-sensical (an empty window) so the helper
        # clamps to "today only" instead of returning end < start.
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=MAY_28, days=0
        )
        self.assertEqual(start, MAY_28)
        self.assertEqual(end, MAY_28)


class ToWindowTest(unittest.TestCase):

    def test_inclusive_end_becomes_exclusive_midnight(self) -> None:
        # `analytics_read` uses `ts < end`; midnight on the day after
        # `end_date` is what makes events from `end_date` visible.
        _, dashboard = _reload()
        window = dashboard.to_window(MAY_1, date(2026, 5, 3))
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
        window = dashboard.to_window(date(2026, 5, 5), MAY_1)
        self.assertEqual(window.start.date(), MAY_1)
        self.assertEqual(window.end.date(), date(2026, 5, 6))

    def test_single_day_window(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(MAY_1, MAY_1)
        self.assertEqual(
            window.start, datetime(2026, 5, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(
            window.end, datetime(2026, 5, 2, tzinfo=timezone.utc)
        )


class ParseIssueNumberTest(unittest.TestCase):

    def test_bare_int(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.parse_issue_number("42"), ISSUE_NUMBER)

    def test_hash_prefix_and_whitespace(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.parse_issue_number(" #42 "), ISSUE_NUMBER)
        self.assertEqual(dashboard.parse_issue_number("# 42"), ISSUE_NUMBER)

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
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertEqual(
            dashboard.db_unconfigured_message(),
            dashboard.UNCONFIGURED_DB_MESSAGE,
        )

    def test_disable_sentinel_returns_message(self) -> None:
        # Each of the documented disable sentinels collapses to None
        # inside `config`, so the helper should treat them the same.
        for sentinel in ("off", "disabled", "none", "OFF", "Disabled"):
            with self.subTest(sentinel=sentinel):
                _, dashboard = _reload({ANALYTICS_DB_URL_ENV: sentinel})
                self.assertEqual(
                    dashboard.db_unconfigured_message(),
                    dashboard.UNCONFIGURED_DB_MESSAGE,
                )

    def test_configured_url_returns_none(self) -> None:
        _, dashboard = _reload(
            {ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL}
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
            module_name: module for module_name, module in sys.modules.items()
            if module_name == "orchestrator" or module_name.startswith("orchestrator.")
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
            for module_name in list(sys.modules):
                if module_name == "orchestrator" or module_name.startswith("orchestrator."):
                    del sys.modules[module_name]

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
            for module_name in list(sys.modules):
                if module_name == "orchestrator" or module_name.startswith("orchestrator."):
                    del sys.modules[module_name]
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
        extent = self._extent(MAY_1, MAY_28)
        window = dashboard.preset_window(dashboard.PRESET_3D, extent)
        self.assertIsNotNone(window)
        # Three-day preset spans the max date and the two days before
        # it, exclusive end at midnight the day after the max.
        self.assertEqual(window.start.date(), MAY_26)
        self.assertEqual(window.end.date(), MAY_29)

    def test_seven_day_preset_anchors_at_max(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(MAY_1, MAY_28)
        window = dashboard.preset_window(dashboard.PRESET_7D, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), MAY_22)
        self.assertEqual(window.end.date(), MAY_29)

    def test_seven_day_preset_clamps_to_min(self) -> None:
        # Data extent is only 3 days wide -- "Last 7 days" must
        # clamp the start at the data extent's min, not reach
        # before it.
        _, dashboard = _reload()
        extent = self._extent(MAY_26, MAY_28)
        window = dashboard.preset_window(dashboard.PRESET_7D, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), MAY_26)
        self.assertEqual(window.end.date(), MAY_29)

    def test_all_preset_covers_full_extent(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(date(2026, 1, 1), MAY_28)
        window = dashboard.preset_window(dashboard.PRESET_ALL, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), date(2026, 1, 1))
        self.assertEqual(window.end.date(), MAY_29)

    def test_custom_preset_returns_none(self) -> None:
        # The caller renders a date-range picker when the preset is
        # `Custom`; `preset_window` returns `None` so the caller can
        # branch on a falsy value rather than special-casing the
        # preset string in two places.
        _, dashboard = _reload()
        extent = self._extent(MAY_1, MAY_28)
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
        extent = self._extent(MAY_1, MAY_28)
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
        win = dashboard.to_window(MAY_1, MAY_7)
        prev = dashboard.previous_window(win)
        self.assertEqual(prev.end, win.start)
        self.assertEqual(prev.end - prev.start, win.end - win.start)

    def test_seven_day_window_yields_seven_day_previous(self) -> None:
        _, dashboard = _reload()
        win = dashboard.to_window(MAY_22, MAY_28)
        prev = dashboard.previous_window(win)
        # `to_window`'s end is exclusive (one day past `end_date`),
        # so the seven-day window spans 7 calendar days; the previous
        # window starts seven days before the current start.
        self.assertEqual(prev.start.date(), date(2026, 5, 15))
        self.assertEqual(prev.end.date(), MAY_22)


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
        self.assertEqual(dashboard.compute_insights(summary), [])

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
    """The rework KPI tile reads off `rework_totals`. Pin the shape so
    a future tweak does not silently shift which buckets count as
    rework.
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
            first_seen=FIRST_SEEN,
            last_seen=LAST_SEEN,
            latest_stage="implementing",
            agent_exits=1,
            total_cost_usd=cost,
            total_input_tokens=0,
            total_output_tokens=0,
        )

    def test_sorts_by_cost_desc(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue(REPO_A, 1, 0.10),
            self._issue(REPO_B, 2, 1.00),
            self._issue(REPO_C, 3, 0.50),
        ]
        top = dashboard.top_expensive_issues(rows, limit=2)
        self.assertEqual([(r.repo, r.issue) for r in top],
                         [(REPO_B, 2), (REPO_C, 3)])

    def test_none_cost_sorts_last(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue(REPO_A, 1, None),
            self._issue(REPO_B, 2, 0.10),
        ]
        top = dashboard.top_expensive_issues(rows, limit=5)
        self.assertEqual([r.issue for r in top], [2, 1])

    def test_limit_zero_returns_empty(self) -> None:
        _, dashboard = _reload()
        rows = [self._issue(REPO_A, 1, 0.10)]
        self.assertEqual(dashboard.top_expensive_issues(rows, limit=0), [])

    def test_ties_break_on_event_count_then_identity(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue(REPO_A, 1, 1.00, events=2),
            self._issue(REPO_A, 2, 1.00, events=10),
            self._issue(REPO_B, 1, 1.00, events=2),
        ]
        top = dashboard.top_expensive_issues(rows)
        # Higher event count first, then (repo, issue) ascending.
        self.assertEqual(
            [(r.repo, r.issue) for r in top],
            [(REPO_A, 2), (REPO_A, 1), (REPO_B, 1)],
        )


class IssuesTableHtmlTest(unittest.TestCase):
    """The "Most expensive issues" panel is hand-rolled HTML (rather
    than `st.dataframe`) so it can carry the standalone mock's
    in-row cost bars and clean / fail status pills.
    """

    def _row(self, repo, issue, cost, *, failed=0, max_round=None,
             max_retry=None):
        _, dashboard = _reload()
        from orchestrator.analytics.read import IssueSummaryRow
        return IssueSummaryRow(
            repo=repo,
            issue=issue,
            event_count=10,
            first_seen=FIRST_SEEN,
            last_seen=LAST_SEEN,
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
        rows = [self._row(REPO_A, 1, 12.0)]
        html = dashboard._issues_table_html(rows)
        for header in ("Issue", "Cost", "Runs", "Review rds",
                       "Retries", "Status"):
            self.assertIn(f">{header}<", html)

    def test_status_pill_renders_clean_when_no_failures(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, 4.0, failed=0)]
        html = dashboard._issues_table_html(rows)
        self.assertIn('class="orch-pill ok"', html)
        self.assertIn(">clean<", html)
        self.assertNotIn('class="orch-pill bad"', html)

    def test_status_pill_renders_fail_when_failures_present(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, 4.0, failed=3)]
        html = dashboard._issues_table_html(rows)
        self.assertIn('class="orch-pill bad"', html)
        self.assertIn(">3 fail<", html)

    def test_in_row_cost_bar_relative_to_max(self) -> None:
        # Cheapest issue's bar is a fraction of the most expensive
        # issue's full-width bar.
        _, dashboard = _reload()
        rows = [
            self._row(REPO_A, 1, 10.0),
            self._row(REPO_B, 2, 5.0),
        ]
        html = dashboard._issues_table_html(rows)
        # Full-width bar on the most expensive issue and a half-
        # width bar on the cheaper one.
        self.assertIn("width:100.0%", html)
        self.assertIn("width:50.0%", html)

    def test_review_rounds_three_or_more_warn_tone(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, 4.0, max_round=4)]
        html = dashboard._issues_table_html(rows)
        # High-review-round cells get the warn class so the operator
        # can spot rework-heavy issues at a glance.
        self.assertIn('class="orch-badge-warn">4', html)


class SkillTriggersHtmlTest(unittest.TestCase):
    """The "Skill trigger rates" panel is hand-rolled HTML (matching
    the backend-efficiency cards and cost-coverage bar) so the small,
    categorical per-(role, backend) table reads cleanly even when every
    rate is 0% -- the `TRACK_SKILL_TRIGGERS=off` baseline.
    """

    def _row(self, role, backend, runs, skill_runs, triggers):
        from orchestrator.analytics.read import SkillTriggerRateRow
        return SkillTriggerRateRow(
            agent_role=role,
            backend=backend,
            runs=runs,
            skill_runs=skill_runs,
            total_triggers=triggers,
        )

    def test_columns_present(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("developer", "claude", 9, 3, 3)]
        html = dashboard._skill_triggers_html(rows)
        for header in ("Role", "Backend", "Runs", "Skill runs",
                       "Trigger rate", "Triggers"):
            self.assertIn(f">{header}<", html)

    def test_rate_rendered_as_percent(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("developer", "claude", 4, 1, 1)]
        html = dashboard._skill_triggers_html(rows)
        # 1 of 4 runs triggered a skill -> 25%.
        self.assertIn(">25%<", html)

    def test_rate_bar_relative_to_busiest_group(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._row("developer", "claude", 10, 10, 10),  # rate 1.0
            self._row("reviewer", "codex", 10, 5, 5),       # rate 0.5
        ]
        html = dashboard._skill_triggers_html(rows)
        # Full-width bar on the 100%-rate group, half-width on the 50%.
        self.assertIn("width:100.0%", html)
        self.assertIn("width:50.0%", html)

    def test_zero_rate_group_renders_zero_percent(self) -> None:
        # A quiet reviewer (0 skill runs) is a real signal, not a
        # dropped row: it renders as an explicit 0% with an empty bar.
        _, dashboard = _reload()
        rows = [self._row("reviewer", "codex", 5, 0, 0)]
        html = dashboard._skill_triggers_html(rows)
        self.assertIn(">0%<", html)
        self.assertIn("width:0.0%", html)

    def test_role_html_escaped(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("dev<&>", "claude", 1, 0, 0)]
        html = dashboard._skill_triggers_html(rows)
        self.assertIn("dev&lt;&amp;&gt;", html)
        self.assertNotIn("dev<&>", html)


class SkillMatrixHtmlTest(unittest.TestCase):
    """The per-skill trigger matrix is the second table under the
    "Skill trigger rates" panel -- a hand-rolled HTML table over
    `get_skill_trigger_matrix` with one row per
    `(repo, agent_role, backend, skill)` cell. It folds each repo's
    skill catalog into the observed triggers so an offered-but-never-
    triggered skill renders as an explicit `0` cell, and degrades to a
    clear fallback notice when no catalog-backed matrix can be built.
    """

    def _row(self, repo, skill, role, backend, runs, skill_runs=None):
        from orchestrator.analytics.read import SkillTriggerMatrixRow
        return SkillTriggerMatrixRow(
            repo=repo,
            skill=skill,
            agent_role=role,
            backend=backend,
            runs=runs,
            skill_runs=runs if skill_runs is None else skill_runs,
        )

    def test_columns_match_issue_spec(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("owner/repo", "develop", "developer", "claude", 2)]
        html = dashboard._skill_matrix_html(rows)
        for header in ("Repo", "Role", "Backend", "Skill",
                       "Runs", "Runs with skill", "Trigger rate"):
            self.assertIn(f">{header}<", html)

    def test_cell_values_rendered(self) -> None:
        _, dashboard = _reload()
        # Distinct cohort total (Runs) and trigger count (Runs with skill)
        # so both columns are exercised independently.
        rows = [self._row(
            "owner/repo", "develop", "developer", "claude", 5, skill_runs=3,
        )]
        html = dashboard._skill_matrix_html(rows)
        # Full repo path (not just the trailing component) so two repos
        # that share a short name stay distinct in a cross-repo matrix.
        self.assertIn(">owner/repo<", html)
        self.assertIn(">developer<", html)
        self.assertIn(">claude<", html)
        self.assertIn(">develop<", html)
        self.assertIn(">5<", html)
        self.assertIn(">3<", html)
        # Trigger rate is derived from the two counts (3/5) and rounds to
        # a whole percent, matching the aggregate table's format.
        self.assertIn(">60%<", html)

    def test_zero_count_row_renders_explicit_muted_zero(self) -> None:
        # An offered-but-never-triggered catalog cell is a real
        # "offered but quiet" signal, not a dropped row: its "Runs with
        # skill" renders as an explicit (muted) 0 rather than going
        # missing, while the cohort `Runs` total stays a plain number.
        _, dashboard = _reload()
        rows = [self._row(
            "owner/repo", "review", "developer", "claude", 4, skill_runs=0,
        )]
        html = dashboard._skill_matrix_html(rows)
        self.assertIn("orch-skillmatrix-zero", html)
        self.assertIn(">0<", html)
        # The derived trigger rate is `0%` and muted on the same signal,
        # so an offered-but-quiet cell reads consistently across columns.
        self.assertIn('<span class="orch-skillmatrix-zero">0%</span>', html)
        # The cohort total is not muted -- it is a plain right-aligned cell.
        self.assertIn('<td class="r">4</td>', html)

    def test_repo_role_backend_skill_html_escaped(self) -> None:
        # Every free-text cell is HTML-escaped so a skill / repo / role
        # name carrying markup cannot break out of the table.
        _, dashboard = _reload()
        rows = [self._row("o/<r&>", "sk<i>ll", "dev<&>", "back<end>", 1)]
        html = dashboard._skill_matrix_html(rows)
        self.assertIn("o/&lt;r&amp;&gt;", html)
        self.assertIn("sk&lt;i&gt;ll", html)
        self.assertIn("dev&lt;&amp;&gt;", html)
        self.assertIn("back&lt;end&gt;", html)
        self.assertNotIn("<r&>", html)
        self.assertNotIn("dev<&>", html)

    def test_empty_rows_render_fallback_not_table(self) -> None:
        # No catalog records matched and no run fired a skill -> there
        # is no catalog-backed matrix to build, so a clear fallback
        # notice renders in place of the table.
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html([])
        self.assertIn("orch-skillmatrix-empty", html)
        self.assertIn("No catalog-backed skill matrix", html)
        # Names the opt-in switch so a quiet panel is not mistaken for a
        # bug, mirroring the aggregate table's caption.
        self.assertIn("TRACK_SKILL_TRIGGERS", html)
        # The table markup itself is not emitted on the fallback path.
        self.assertNotIn("<table", html)

    def test_fallback_message_is_html_escaped(self) -> None:
        # The fallback message is escaped before it lands in the div, so
        # the apostrophe-carrying copy renders without breaking out.
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html([])
        self.assertIn("&#x27;", html)


class SkillMatrixSortTest(unittest.TestCase):
    """The per-skill trigger matrix column headers are clickable sort
    controls: each is an anchor writing `mtx_sort` / `mtx_dir` query
    params, and the caller feeds the parsed `(column, direction)` back
    into `_skill_matrix_html` so the rows re-sort on that column and the
    active header shows a ▲ / ▼ indicator.
    """

    def _row(self, repo, skill, role, backend, runs, skill_runs=None):
        from orchestrator.analytics.read import SkillTriggerMatrixRow
        return SkillTriggerMatrixRow(
            repo=repo,
            skill=skill,
            agent_role=role,
            backend=backend,
            runs=runs,
            skill_runs=runs if skill_runs is None else skill_runs,
        )

    def _rows(self):
        # Distinct repo / runs values per row so an ordering assertion can
        # key off either without ambiguity.
        return [
            self._row("b/repo", "alpha", "developer", "claude", 2, 1),
            self._row("a/repo", "beta", "reviewer", "codex", 9, 9),
            self._row("c/repo", "gamma", "developer", "claude", 5, 0),
        ]

    def test_headers_are_clickable_self_targeting_sort_links(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html(self._rows())
        # Every column is an in-tab anchor pointing at its own sort param.
        for key in MTX_SORT_KEYS:
            self.assertIn(f"?{MTX_SORT_PARAM}={key}&{MTX_DIR_PARAM}=", html)
        self.assertIn('target="_self"', html)
        # Text columns default a first click to ascending, numeric ones to
        # descending (largest first is the interesting end for counts).
        self.assertIn(f"?{MTX_SORT_PARAM}=repo&{MTX_DIR_PARAM}={SORT_ASC}", html)
        self.assertIn(
            f"?{MTX_SORT_PARAM}=runs&{MTX_DIR_PARAM}={SORT_DESC}", html
        )
        # With no active sort no header carries a direction indicator (the
        # class still appears in the CSS block, so match the span markup).
        self.assertNotIn('<span class="orch-skillmatrix-sort">', html)

    def test_active_descending_column_shows_down_arrow_and_flips(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html(
            self._rows(), sort_key="runs", descending=True,
        )
        # Exactly one column is marked active, and it shows the ▼ arrow.
        self.assertEqual(
            html.count('<span class="orch-skillmatrix-sort">'), 1,
        )
        self.assertIn(
            '<span class="orch-skillmatrix-sort">▼</span>', html,
        )
        # Re-clicking the active (descending) column flips it to ascending.
        self.assertIn(
            f"?{MTX_SORT_PARAM}=runs&{MTX_DIR_PARAM}={SORT_ASC}", html
        )

    def test_active_ascending_column_shows_up_arrow_and_flips(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html(
            self._rows(), sort_key="repo", descending=False,
        )
        self.assertIn(
            '<span class="orch-skillmatrix-sort">▲</span>', html,
        )
        self.assertIn(
            f"?{MTX_SORT_PARAM}=repo&{MTX_DIR_PARAM}={SORT_DESC}", html
        )

    def test_rows_render_in_selected_column_order(self) -> None:
        _, dashboard = _reload()
        asc = dashboard._skill_matrix_html(
            self._rows(), sort_key="runs", descending=False,
        )
        # runs 2 < 5 < 9 -> repos b, c, a in that order.
        self.assertLess(asc.index(">b/repo<"), asc.index(">c/repo<"))
        self.assertLess(asc.index(">c/repo<"), asc.index(">a/repo<"))
        desc = dashboard._skill_matrix_html(
            self._rows(), sort_key="runs", descending=True,
        )
        self.assertLess(desc.index(">a/repo<"), desc.index(">c/repo<"))
        self.assertLess(desc.index(">c/repo<"), desc.index(">b/repo<"))

    def test_unsorted_render_defaults_repo_asc_then_rate_desc(self) -> None:
        # No sort key -> the default view orders rows by repo ascending,
        # then trigger rate descending within each repo, so each repo's
        # hottest skills lead. Two rows share a repo with different rates
        # so both keys are exercised (skills identify the rows uniquely).
        _, dashboard = _reload()
        rows = [
            self._row("b/repo", "alpha", "developer", "claude", 4, 1),
            self._row("a/repo", "beta", "developer", "claude", 4, 1),
            self._row("a/repo", "gamma", "reviewer", "codex", 4, 3),
        ]
        html = dashboard._skill_matrix_html(rows)
        # Within a/repo, rate descending: gamma (75%) precedes beta (25%).
        self.assertLess(
            html.index(">gamma<"), html.index(">beta<"),
        )
        # Repo ascending: the a/repo rows precede the b/repo row.
        self.assertLess(
            html.index(">beta<"), html.index(">alpha<"),
        )

    def test_sort_helper_unknown_key_is_identity(self) -> None:
        from orchestrator import dashboard_html
        rows = self._rows()
        sorted_rows = dashboard_html._sort_skill_matrix_rows(rows, None, False)
        self.assertEqual(sorted_rows, rows)
        sorted_rows = dashboard_html._sort_skill_matrix_rows(rows, "bogus", True)
        self.assertEqual(sorted_rows, rows)

    def test_parse_matrix_sort_from_query_params(self) -> None:
        _, dashboard = _reload()
        cases = [
            ({}, (None, False)),
            ({MTX_SORT_PARAM: "runs"}, ("runs", False)),
            ({MTX_SORT_PARAM: "runs", MTX_DIR_PARAM: SORT_DESC}, ("runs", True)),
            ({MTX_SORT_PARAM: "runs", MTX_DIR_PARAM: SORT_ASC}, ("runs", False)),
            ({MTX_SORT_PARAM: "rate", MTX_DIR_PARAM: SORT_DESC}, ("rate", True)),
            # An unknown / stale column degrades to the default order
            # rather than raising.
            ({MTX_SORT_PARAM: "bogus", MTX_DIR_PARAM: SORT_DESC}, (None, False)),
            ({MTX_DIR_PARAM: SORT_DESC}, (None, False)),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(
                    dashboard.parse_skill_matrix_sort(params), expected,
                )


class DeltaPillTest(unittest.TestCase):
    """KPI delta pills must paint cost / token increases red and
    drops green. An earlier draft mapped `invert=True && value > 0`
    to the `.down` class (green) for "Total spend" and "Total
    tokens", which painted rising cost green -- backwards for a
    cost dashboard. The fix drops `invert=True` from those KPIs so
    the default mapping (up=red, down=green) lands.
    """

    def test_positive_default_paints_up_red_arrow(self) -> None:
        _, dashboard = _reload()
        html = dashboard._delta_pill(0.25)
        self.assertIn('orch-delta up', html)
        self.assertIn('▲', html)

    def test_negative_default_paints_down_green_arrow(self) -> None:
        _, dashboard = _reload()
        html = dashboard._delta_pill(-0.25)
        self.assertIn('orch-delta down', html)
        self.assertIn('▼', html)

    def test_invert_swaps_only_color_not_arrow(self) -> None:
        # `invert=True` reserved for "up is good" KPIs (issues
        # resolved, success rate). The arrow still follows the
        # value's sign so the direction is unambiguous, but the
        # color swaps so positive growth reads as green.
        _, dashboard = _reload()
        pos = dashboard._delta_pill(0.25, invert=True)
        neg = dashboard._delta_pill(-0.25, invert=True)
        self.assertIn('orch-delta down', pos)
        self.assertIn('▲', pos)
        self.assertIn('orch-delta up', neg)
        self.assertIn('▼', neg)

    def test_none_renders_nothing(self) -> None:
        # No prior window to compare against: the grey placeholder pill
        # read like a (non-functional) minimize control, so the slot is
        # dropped entirely rather than rendering a flat dash.
        _, dashboard = _reload()
        self.assertEqual(dashboard._delta_pill(None), "")

    def test_zero_delta_renders_nothing(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard._delta_pill(0.0), "")


class InsightsHtmlTest(unittest.TestCase):
    """The colored icon carries severity, so the rendered message
    no longer leads with a redundant `Warning.` / `Info.` prefix.
    """

    def test_message_renders_without_severity_lead_in(self) -> None:
        _, dashboard = _reload()
        banner = dashboard.InsightBanner(
            severity="warning",
            message="Agent failure rate >= 10% in this window.",
        )
        html = dashboard._insights_html([banner])
        # The message body lands verbatim (with HTML-escaping) and the
        # severity word is NOT prefixed.
        self.assertIn(
            "Agent failure rate &gt;= 10% in this window.",
            html,
        )
        self.assertNotIn("<strong>Warning.</strong>", html)
        # The CSS class still carries the severity so the colored
        # icon / banner background paints correctly.
        self.assertIn('orch-insight warning', html)


class PlotlyConfigTest(unittest.TestCase):
    """`PLOTLY_CONFIG` is passed to every `st.plotly_chart` so the
    hover modebar (camera / zoom / pan) stays off every card --
    the standalone mock has no chart chrome.
    """

    def test_plotly_config_disables_modebar(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(
            dashboard.PLOTLY_CONFIG.get("displayModeBar"), False
        )


class CacheKeyTest(unittest.TestCase):
    """`st.cache_data` hashes the cache key tuple; lists from
    multiselects need to become tuples, and `None` must be preserved
    so the tri-state filter contract (None / [] / [...]) does not
    collapse at the cache layer.
    """

    def test_lists_become_tuples(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(MAY_1, MAY_7)
        key = dashboard.cache_key(
            window, CACHE_REPO,
            list(EVENT_NAMES), list(STAGE_NAMES), ISSUE_NUMBER,
        )
        self.assertEqual(
            key,
            (
                window.start,
                window.end,
                CACHE_REPO,
                EVENT_NAMES,
                STAGE_NAMES,
                ISSUE_NUMBER,
            ),
        )
        hash(key)  # must be hashable

    def test_none_is_preserved(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(MAY_1, MAY_7)
        key = dashboard.cache_key(window, None, None, None, None)
        self.assertEqual(
            key, (window.start, window.end, None, None, None, None)
        )

    def test_empty_list_distinct_from_none(self) -> None:
        # Empty events / stages mean "cleared multiselect, show
        # nothing"; the cache key must keep the empty tuple distinct
        # from None so the two SQL shapes do not collide in cache.
        _, dashboard = _reload()
        window = dashboard.to_window(MAY_1, MAY_7)
        empty = dashboard.cache_key(window, CACHE_REPO, [], [], None)
        none = dashboard.cache_key(window, CACHE_REPO, None, None, None)
        self.assertNotEqual(empty, none)
        self.assertEqual(empty[3], ())
        self.assertEqual(empty[4], ())


class CachedReadConnectionScopingTest(_MainSourceTest):
    """The redesigned read path reuses a thread-local analytics
    connection across the dashboard's 14 reads instead of opening a
    socket per call (issue #376). The Streamlit cache keys must
    therefore stay connection-free -- a raw `psycopg.Connection` is
    not a hashable cache key and every reload would otherwise look
    like a cache miss.

    These tests inspect the source of `dashboard.main` rather than
    driving it under Streamlit so the suite stays hermetic against
    the dashboard dependency group (Streamlit + Plotly are opt-in).
    """

    def _drilldown_source(self) -> str:
        import inspect
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})
        return inspect.getsource(dashboard._render_drilldown)

    def test_main_uses_analytics_connection_scope(self) -> None:
        src = self._main_source()
        self.assertIn(
            "analytics_read.analytics_connection()", src,
            "dashboard.main must scope reads through "
            "`analytics_connection` so the per-thread persistent "
            "socket is reused across widgets",
        )

    def test_cached_wrappers_do_not_accept_conn_arg(self) -> None:
        # Each `_read_*` wrapper's positional parameter list is the
        # cache key. `conn` must NOT appear there -- it would force
        # st.cache_data to hash a connection object, which crashes
        # on the unhashable psycopg.Connection and (with a stringy
        # fallback) treats every refreshed conn as a cache miss.
        src = self._main_source()
        wrapper_names = [
            "_read_summary",
            "_read_prev_kpi",
            "_read_time_series",
            "_read_stage_breakdown",
            "_read_recent_agent_exits",
            "_read_top_cost_issues",
            "_read_review_round",
            "_read_backend_efficiency",
            "_read_repo_breakdown",
            "_read_cost_coverage",
            "_read_hourly_heatmap",
            "_read_throughput",
            "_read_backend_daily_tokens",
        ]
        for name in wrapper_names:
            with self.subTest(name=name):
                # Each wrapper's signature line lives inside main()'s
                # source; check that none mention `conn` as a
                # parameter (which would land in the cache key).
                marker = f"def {name}("
                self.assertIn(marker, src)
                # Pull the def line(s) up to the closing paren so we
                # can assert `conn` is not in the parameter list. The
                # def signatures are short (one or two lines), so a
                # narrow window around the marker is enough.
                head = src.index(marker)
                tail = src.index("):", head)
                signature = src[head:tail]
                self.assertNotIn(
                    " conn", signature,
                    f"{name} must not accept a `conn` argument "
                    "(it would become part of the cache key)",
                )

    def test_wrappers_pass_conn_kwarg_to_read_helpers(self) -> None:
        # Inside each wrapper's body, the conn from
        # `analytics_connection()` must be forwarded to the read
        # helper -- otherwise we open a new socket per call (the very
        # thing the refactor is supposed to eliminate).
        main_src = self._main_source()
        # 13 cached wrappers (including Layer 3's `_read_prev_kpi`)
        # + the extent / options reads at the top of main() = 15
        # forwards inside main(). The per-issue drill-down lives in
        # `_render_drilldown` and is checked separately.
        self.assertGreaterEqual(
            main_src.count("conn=conn"), 15,
            "every read inside main() should forward the scoped "
            "connection from `analytics_connection`",
        )
        drilldown_src = self._drilldown_source()
        self.assertIn("analytics_read.analytics_connection()", drilldown_src)
        self.assertIn("conn=conn", drilldown_src)

    def test_prev_summary_reader_uses_lightweight_kpi_path(self) -> None:
        # Layer 3 split the previous-window read off `get_summary`
        # so the dashboard only pays for the scalars it actually
        # reads off `prev_summary` (cost / token totals + agent-run
        # count for KPI delta pills and the cost-trend banner). The
        # `_read_prev_kpi` wrapper must therefore call
        # `analytics_read.get_kpi_prev` rather than reusing the
        # full `get_summary` shape -- if it falls back to the heavy
        # path, the cold-load wins from Layer 3 evaporate.
        src = self._main_source()
        marker = "def _read_prev_kpi("
        self.assertIn(marker, src)
        head = src.index(marker)
        # The wrapper body is short; walk to the next `def` (which
        # bounds the cached wrapper region) so the substring search
        # below cannot accidentally catch a later wrapper.
        body = src[head:src.index("\n    def ", head + 1)]
        self.assertIn("analytics_read.get_kpi_prev(", body)
        self.assertNotIn("analytics_read.get_summary(", body)
        # And the `prev_summary` entry in the reader fan-out must
        # dispatch through `_read_prev_kpi` so the lightweight path
        # is the one that actually fires when the dashboard renders.
        self.assertIn(
            '("prev_summary", lambda: _read_prev_kpi(*prev_key))',
            src,
        )


class AnalyticsConnectionExposureTest(unittest.TestCase):
    """`analytics_connection` and `close_thread_local_connection` are
    the new public surface from `analytics_read`. The dashboard
    imports the module wholesale (`from orchestrator.analytics import
    read as analytics_read`), so the symbols must be reachable as
    attributes for both `with analytics_read.analytics_connection()`
    and any shutdown hook that wants to drain the thread-local.
    """

    def test_analytics_connection_is_a_context_manager(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertTrue(
            hasattr(dashboard.analytics_read, "analytics_connection")
        )
        self.assertTrue(
            hasattr(
                dashboard.analytics_read, "close_thread_local_connection"
            )
        )
        # Quick smoke: the unset-URL branch yields None without
        # touching any connect factory.
        with dashboard.analytics_read.analytics_connection() as conn:
            self.assertIsNone(conn)


class DashboardParallelReadsEnabledTest(unittest.TestCase):
    """`DASHBOARD_PARALLEL_READS` is the A/B knob for the parallel
    read fan-out. Default off so the sequential behavior holds until
    an operator opts in; truthy spellings follow the same vocabulary
    as the rest of the codebase (`DECOMPOSE=on` etc.).
    """

    def test_unset_defaults_to_false(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertFalse(dashboard.dashboard_parallel_reads_enabled())

    def test_empty_string_is_false(self) -> None:
        _, dashboard = _reload(
            {ANALYTICS_DB_URL_ENV: "", PARALLEL_READS_ENV: ""}
        )
        self.assertFalse(dashboard.dashboard_parallel_reads_enabled())

    def test_truthy_spellings_enable_parallel(self) -> None:
        # The four documented truthy sentinels all enable the fan-out
        # regardless of case so an operator can paste whichever spelling
        # their team's playbook uses.
        for sentinel in ("1", "true", "on", "yes", "ON", "Yes", "TRUE"):
            with self.subTest(sentinel=sentinel):
                _, dashboard = _reload(
                    {
                        ANALYTICS_DB_URL_ENV: "",
                        PARALLEL_READS_ENV: sentinel,
                    }
                )
                self.assertTrue(
                    dashboard.dashboard_parallel_reads_enabled()
                )

    def test_falsy_spellings_keep_sequential(self) -> None:
        for sentinel in ("0", "false", "off", "no", "disabled", "none"):
            with self.subTest(sentinel=sentinel):
                _, dashboard = _reload(
                    {
                        ANALYTICS_DB_URL_ENV: "",
                        PARALLEL_READS_ENV: sentinel,
                    }
                )
                self.assertFalse(
                    dashboard.dashboard_parallel_reads_enabled()
                )

    def test_whitespace_is_stripped(self) -> None:
        # Operators paste env values from playbooks; tolerate leading /
        # trailing whitespace so a stray newline does not silently fall
        # back to the sequential path.
        _, dashboard = _reload(
            {ANALYTICS_DB_URL_ENV: "", PARALLEL_READS_ENV: "  on  "}
        )
        self.assertTrue(dashboard.dashboard_parallel_reads_enabled())


class FacadeReExportCompatibilityTest(unittest.TestCase):
    """The helper split moved the pure logic into `dashboard_state` /
    `dashboard_kpis` / `dashboard_html`, but `orchestrator.dashboard`
    must keep re-exporting every name that lived on it on `origin/main`
    -- including the module-private `_parse_parallel_reads_flag` /
    `_TRUTHY` the parallel-reads knob is parsed through -- so
    `from orchestrator.dashboard import <helper>` keeps resolving
    against the facade rather than raising `ImportError`.
    """

    def test_parallel_reads_internals_reexported_from_state(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        import orchestrator.dashboard_state as state
        # Each facade name is the very object the extracted module
        # defines -- a genuine re-export, not a shadow copy.
        self.assertIs(
            dashboard._parse_parallel_reads_flag,
            state._parse_parallel_reads_flag,
        )
        self.assertIs(dashboard._TRUTHY, state._TRUTHY)
        # And the re-exported helper still works through the alias.
        self.assertIsInstance(dashboard._parse_parallel_reads_flag(), bool)


class FanOutReadsSequentialTest(unittest.TestCase):
    """The sequential branch of `_fan_out_reads` runs each reader in
    submission order on the calling thread and returns results keyed
    by reader name. The helper exists so the `main()` call site can
    collapse 13 lines of `name = _read_name(*key)` into one dispatch
    and so tests can inject fake readers without booting Streamlit.
    """

    def test_results_keyed_by_name_in_submission_order(self) -> None:
        _, dashboard = _reload()
        order: list[str] = []

        def _make(name: str, value: int):
            def fn():
                order.append(name)
                return value
            return fn

        readers = [
            ("a", _make("a", 1)),
            ("b", _make("b", 2)),
            ("c", _make("c", 3)),
        ]
        results = dashboard._fan_out_reads(readers, parallel=False)
        self.assertEqual(results, {"a": 1, "b": 2, "c": 3})
        # Sequential path runs in submission order so a deterministic
        # log line / error message references the right reader.
        self.assertEqual(order, ["a", "b", "c"])

    def test_first_failing_reader_propagates(self) -> None:
        # Sequential path stops at the first error so the caller
        # surfaces one user-friendly message instead of a stack of
        # errors.
        _, dashboard = _reload()
        from orchestrator.analytics.read import AnalyticsReadError
        called: list[str] = []

        def _ok():
            called.append("ok")
            return 1

        def _boom():
            called.append("boom")
            raise AnalyticsReadError("connection refused")

        def _never():
            called.append("never")
            return 2

        readers = [("a", _ok), ("b", _boom), ("c", _never)]
        with self.assertRaises(AnalyticsReadError):
            dashboard._fan_out_reads(readers, parallel=False)
        self.assertEqual(called, ["ok", "boom"])

    def test_each_reader_runs_exactly_once(self) -> None:
        _, dashboard = _reload()
        counts = {"a": 0, "b": 0}

        def _mk(name):
            def fn():
                counts[name] += 1
                return name
            return fn

        readers = [("a", _mk("a")), ("b", _mk("b"))]
        dashboard._fan_out_reads(readers, parallel=False)
        self.assertEqual(counts, {"a": 1, "b": 1})


class FanOutReadsParallelTest(unittest.TestCase):
    """The parallel branch dispatches readers across a
    `ThreadPoolExecutor`. Each worker thread is responsible for its
    own analytics connection (the thread-local cache from #383); the
    helper itself only owns dispatch + result collection.
    """

    def test_all_results_returned_keyed_by_name(self) -> None:
        _, dashboard = _reload()

        def _mk(value):
            def fn():
                return value
            return fn

        readers = [(f"r{i}", _mk(i)) for i in range(5)]
        results = dashboard._fan_out_reads(
            readers, parallel=True, max_workers=4
        )
        self.assertEqual(
            results, {f"r{i}": i for i in range(5)}
        )

    def test_each_reader_runs_exactly_once_on_a_worker(self) -> None:
        # Re-entrant workers must not re-submit a reader (and
        # the dispatch logic must not double-collect). The set of
        # observed thread ids should be > 1 to confirm actual
        # parallelism, but the exact count depends on scheduling so
        # we only assert it ran on a non-main thread when more than
        # one reader was submitted.
        import threading
        _, dashboard = _reload()
        calls: dict[str, int] = {}
        threads: set[int] = set()
        lock = threading.Lock()

        def _mk(name):
            def fn():
                with lock:
                    calls[name] = calls.get(name, 0) + 1
                    threads.add(threading.get_ident())
                return name
            return fn

        readers = [(f"r{i}", _mk(f"r{i}")) for i in range(8)]
        dashboard._fan_out_reads(
            readers, parallel=True, max_workers=4
        )
        self.assertEqual(set(calls.values()), {1})
        self.assertEqual(set(calls), {f"r{i}" for i in range(8)})
        self.assertNotIn(threading.get_ident(), threads)

    def test_parallel_wall_clock_beats_sequential_sum(self) -> None:
        # Smoke: with 4 workers and 4 readers each sleeping ~80 ms, the
        # wall-clock should be much closer to one reader's runtime than
        # to the sum. Pin a loose ceiling so the test is not flaky on a
        # busy CI host but still fails if the executor degenerates to
        # the sequential path.
        import time
        _, dashboard = _reload()
        delay = 0.08

        def _slow():
            time.sleep(delay)
            return "ok"

        readers = [(f"r{i}", _slow) for i in range(4)]
        t0 = time.perf_counter()
        results = dashboard._fan_out_reads(
            readers, parallel=True, max_workers=4
        )
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(results), 4)
        # Sequential sum would be 4 * delay = 320 ms; one wave on
        # four workers should land well under 2 * delay.
        self.assertLess(elapsed, delay * 2.5)

    def test_reader_exception_propagates(self) -> None:
        # `AnalyticsReadError` raised in a worker must surface from
        # the helper so the caller's `try/except AnalyticsReadError`
        # in `main()` can render a single `st.error` and stop.
        _, dashboard = _reload()
        from orchestrator.analytics.read import AnalyticsReadError

        def _boom():
            raise AnalyticsReadError("query failed")

        def _ok():
            return 1

        readers = [("ok", _ok), ("boom", _boom)]
        with self.assertRaises(AnalyticsReadError) as cm:
            dashboard._fan_out_reads(
                readers, parallel=True, max_workers=2
            )
        self.assertIn("query failed", str(cm.exception))


class MainParallelFanOutWiringTest(_MainSourceTest):
    """`main()` must dispatch the 14 widget reads through
    `_fan_out_reads`, drive the parallel switch off the env-backed
    helper, and emit a single `dashboard.load:` INFO line so the A/B
    rollout has a measurement surface. Streamlit is not installed for
    the default `uv sync --locked`, so these tests inspect the
    `main()` source rather than driving it under Streamlit.
    """

    def test_main_uses_fan_out_helper(self) -> None:
        src = self._main_source()
        self.assertIn("_fan_out_reads(", src)

    def test_main_drives_parallel_off_env_helper(self) -> None:
        src = self._main_source()
        # The env-backed helper is the single source of truth for the
        # flag so a test or shutdown hook can flip it without
        # rewriting `main()`.
        self.assertIn("dashboard_parallel_reads_enabled()", src)

    def test_main_emits_load_timing_log(self) -> None:
        src = self._main_source()
        # The instrumentation line carries total wall-clock, reader
        # count, and the parallel flag so the operator can A/B with a
        # single grep.
        self.assertIn("dashboard.load:", src)
        self.assertIn("perf_counter()", src)

    def test_main_catches_analytics_read_error_around_fan_out(self) -> None:
        # The fan-out is wrapped in the same `try/except
        # AnalyticsReadError` the sequential path used, so a worker
        # exception still surfaces as one `st.error`. The staged
        # split (issue #379) calls `_fan_out_reads` twice -- once
        # for the topbar / KPI inputs and once for the rest of the
        # widgets -- so the wiring assertion is that the fan-out
        # helper is the only dispatch surface and that the read
        # error type is caught around it.
        src = self._main_source()
        self.assertIn(
            "_fan_out_reads(\n                first_wave_readers", src
        )
        self.assertIn(
            "_fan_out_reads(\n                    second_wave_readers", src
        )
        # Two `try/except AnalyticsReadError` blocks -- one per wave
        # -- so a worker exception in either wave surfaces as one
        # `st.error` and stops the render.
        self.assertGreaterEqual(
            src.count("analytics_read.AnalyticsReadError"), 2,
        )


class SkillMatrixWiringTest(_MainSourceTest):
    """`main()` wires the per-skill trigger matrix through the same
    cached / fan-out read pattern as every other widget and renders it
    as the second table under the existing "Skill trigger rates"
    aggregate. Streamlit is not installed for the default sync, so these
    inspect `main()`'s source rather than driving it under Streamlit.
    """

    def test_matrix_read_calls_matrix_read_model(self) -> None:
        src = self._main_source()
        self.assertIn("def _read_skill_trigger_matrix(", src)
        self.assertIn("analytics_read.get_skill_trigger_matrix(", src)

    def test_matrix_read_forwards_scoped_connection(self) -> None:
        # Reuse the cached-read pattern: the scoped thread-local
        # connection is forwarded to the read helper so the matrix read
        # shares the open socket rather than opening its own.
        src = self._main_source()
        marker = "def _read_skill_trigger_matrix("
        head = src.index(marker)
        body = src[head:src.index("\n\n", head)]
        self.assertIn("analytics_read.analytics_connection()", body)
        self.assertIn("conn=conn", body)

    def test_matrix_read_wrapper_takes_no_conn_in_cache_key(self) -> None:
        # `conn` must not appear in the wrapper's parameter list -- it
        # would land in the `st.cache_data` key and crash on the
        # unhashable psycopg connection.
        src = self._main_source()
        marker = "def _read_skill_trigger_matrix("
        head = src.index(marker)
        tail = src.index("):", head)
        self.assertNotIn(" conn", src[head:tail])

    def test_matrix_dispatched_in_second_wave(self) -> None:
        src = self._main_source()
        self.assertIn(
            '("skill_matrix_rows", '
            "lambda: _read_skill_trigger_matrix(*key))",
            src,
        )

    def test_matrix_rendered_as_second_table_under_aggregate(self) -> None:
        # The matrix is the SECOND table: it renders after the aggregate
        # `_skill_triggers_html(skill_rows)` table, inside the same card.
        src = self._main_source()
        agg = src.index("_skill_triggers_html(skill_rows)")
        matrix = src.index("_skill_matrix_html(")
        self.assertLess(agg, matrix)

    def test_matrix_only_renders_when_aggregate_has_rows(self) -> None:
        # The matrix render sits inside the `if skill_rows:` branch, so
        # the empty-panel path still shows the single no-rows notice
        # rather than a fallback for each table.
        src = self._main_source()
        branch = src.index("if skill_rows:")
        else_branch = src.index(
            'st.info("No `agent_exit` rows match the current filters.")',
            branch,
        )
        matrix = src.index("_skill_matrix_html(")
        self.assertLess(branch, matrix)
        self.assertLess(matrix, else_branch)

    def test_matrix_rendered_inside_collapsed_expander(self) -> None:
        # The matrix folds into a collapsed expander (mirroring the
        # "Recent agent runs" block) so it does not dominate the card by
        # default. The `_skill_matrix_html` render must sit after an
        # `st.expander(..., expanded=False)` opened for the matrix.
        src = self._main_source()
        expander = src.index('with st.expander(\n                "Per-skill')
        matrix = src.index("_skill_matrix_html(")
        self.assertLess(expander, matrix)
        # The expander block carrying the matrix opens collapsed.
        block = src[expander:matrix]
        self.assertIn("expanded=False", block)


class StaticMetadataCacheTest(_MainSourceTest):
    """`get_data_extent` and `get_filter_options` (issue #379) carry
    no filter inputs and only change as `analytics.sync` ingests new
    events, so the dashboard wraps them in `@st.cache_data` under the
    longer `STATIC_METADATA_TTL_SECONDS` (5 min) instead of the
    per-filter 60 s TTL. Together these collapse the topbar / sidebar
    round-trip on every Streamlit rerun.
    """

    def test_ttl_is_five_minutes(self) -> None:
        # Pin the constant so a future tweak changes it deliberately.
        # A 5-minute TTL is long enough to absorb the typical rerun
        # cadence (Streamlit rerenders on every widget interaction)
        # but short enough that a freshly-synced repo / event value
        # surfaces within one `analytics.sync` cycle.
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertEqual(dashboard.STATIC_METADATA_TTL_SECONDS, 300)

    def test_extent_reader_decorated_with_longer_ttl(self) -> None:
        src = self._main_source()
        marker = "def _read_data_extent("
        self.assertIn(marker, src)
        # The cached wrapper must sit directly under
        # `@st.cache_data(... ttl=STATIC_METADATA_TTL_SECONDS)` --
        # not the 60 s TTL the per-filter wrappers use.
        head = src.index(marker)
        # Look back to the decorator just above the def.
        decorator_window = src[max(0, head - 200):head]
        self.assertIn("@st.cache_data(", decorator_window)
        self.assertIn(
            "ttl=STATIC_METADATA_TTL_SECONDS", decorator_window
        )
        self.assertIn("show_spinner=False", decorator_window)

    def test_filter_options_reader_decorated_with_longer_ttl(self) -> None:
        src = self._main_source()
        marker = "def _read_filter_options("
        self.assertIn(marker, src)
        head = src.index(marker)
        decorator_window = src[max(0, head - 200):head]
        self.assertIn("@st.cache_data(", decorator_window)
        self.assertIn(
            "ttl=STATIC_METADATA_TTL_SECONDS", decorator_window
        )
        self.assertIn("show_spinner=False", decorator_window)

    def test_extent_and_options_readers_take_no_args(self) -> None:
        # The static-metadata readers carry no filter inputs, so the
        # cache key is empty -- they tolerate the longer TTL because
        # the values only change as `analytics.sync` ingests new
        # events, not when the operator adjusts the filter bar. Pin
        # the empty signature so a future refactor cannot silently
        # re-introduce a parameter (e.g. a connection) that would
        # turn into part of the cache key.
        src = self._main_source()
        for marker in ("def _read_data_extent(", "def _read_filter_options("):
            with self.subTest(marker=marker):
                head = src.index(marker)
                tail = src.index("):", head)
                self.assertEqual(src[head:tail + 1], marker + ")")

    def test_main_dispatches_through_cached_readers(self) -> None:
        # The bare `get_data_extent(conn=conn)` / `get_filter_options
        # (conn=conn)` calls the old code paid on every rerun must be
        # gone -- the only call sites for those reads now live inside
        # the cached wrappers' bodies (where they are intentionally
        # routed through the thread-local connection).
        src = self._main_source()
        # `main` itself calls the cached wrappers, not the raw reads.
        self.assertIn("extent = _read_data_extent()", src)
        self.assertIn("options = _read_filter_options()", src)


class StagedRenderTest(_MainSourceTest):
    """Issue #379 splits the read fan-out into two staged waves so
    the topbar / filter meta / insight banners / KPI strip paint as
    soon as their inputs are available, rather than blocking on every
    widget. The first wave covers the six reads those above-the-fold
    widgets consume; the second wave covers the eight remaining
    widget reads. Worker threads only return data; every `st.*` /
    placeholder write happens on the main render thread between the
    two waves.
    """

    def _wave_block(self, src: str, name: str) -> str:
        # The reader lists are short, indented at the function-body
        # level (4 spaces), and bracketed by `[` ... `\n    ]`. The
        # type annotation `list[tuple[str, Callable[[], Any]]]` sits
        # on the assignment line, so we walk past the `= [` opening
        # and stop at the first dedented `]` (preceded by the body
        # indent) to extract just the list literal.
        marker = f"{name}: list"
        self.assertIn(marker, src, f"{name} declaration missing")
        head = src.index("= [", src.index(marker)) + len("= [")
        tail = src.index("\n    ]", head)
        return src[head:tail]

    def test_first_wave_carries_only_kpi_topbar_inputs(self) -> None:
        # The six reads in the first wave are exactly the inputs the
        # topbar / filter meta / insight banners / KPI strip consume.
        # Pin the set so a future refactor that adds (or drops) a
        # reader has to update the staging explicitly.
        src = self._main_source()
        wave = self._wave_block(src, "first_wave_readers")
        for name in FIRST_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', wave)
        # The remaining widget reads are NOT in the first wave -- they
        # would force the spinner to wait for the slowest widget read
        # before the KPI strip can paint.
        for name in SECOND_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(f'"{name}"', wave)

    def test_second_wave_carries_the_remaining_widget_reads(self) -> None:
        src = self._main_source()
        wave = self._wave_block(src, "second_wave_readers")
        for name in SECOND_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', wave)
        # And the topbar / KPI-strip inputs are NOT in the second
        # wave -- they belong to the first wave so the strip can
        # paint before the slow widget reads finish.
        for name in FIRST_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(f'"{name}"', wave)

    def test_topbar_and_meta_render_between_waves(self) -> None:
        # `topbar_slot.markdown` and `meta_slot.markdown` (which fill
        # the above-the-fold content) must happen AFTER the first
        # wave dispatch and BEFORE the second wave dispatch.
        # Otherwise the staged-render gain evaporates.
        src = self._main_source()
        first = src.index("_fan_out_reads(\n                first_wave_readers")
        second = src.index("_fan_out_reads(\n                    second_wave_readers")
        topbar_render = src.index("topbar_slot.markdown(")
        meta_render = src.index("meta_slot.markdown(")
        kpi_render = src.index("_kpi_strip_html(kpis)")
        self.assertLess(first, topbar_render)
        self.assertLess(topbar_render, second)
        self.assertLess(first, meta_render)
        self.assertLess(meta_render, second)
        self.assertLess(first, kpi_render)
        self.assertLess(kpi_render, second)

    def test_inline_loading_spinner_wraps_fan_out(self) -> None:
        # A single in-line "Loading analytics…" spinner spans both
        # waves so the user gets immediate feedback on a cold load
        # instead of staring at a blank page. Pin the constant +
        # `st.spinner` call so a future refactor cannot silently drop
        # the feedback surface.
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertEqual(
            dashboard.LOADING_INDICATOR_MESSAGE, "Loading analytics…"
        )
        src = self._main_source()
        self.assertIn(
            "with st.spinner(LOADING_INDICATOR_MESSAGE):", src,
        )
        # And the spinner brackets BOTH fan-out calls -- a spinner
        # that only covered the first wave would clear before the
        # second wave painted its widgets.
        spinner_head = src.index(
            "with st.spinner(LOADING_INDICATOR_MESSAGE):"
        )
        first = src.index(
            "_fan_out_reads(\n                first_wave_readers"
        )
        second = src.index(
            "_fan_out_reads(\n                    second_wave_readers"
        )
        self.assertLess(spinner_head, first)
        self.assertLess(spinner_head, second)

    def test_widget_rendering_runs_on_main_thread_not_workers(self) -> None:
        # Worker threads in `_fan_out_reads` only return data -- the
        # `st.*` / `topbar_slot.markdown(...)` calls all live in
        # `main()` itself, on the main render thread. We assert this
        # by checking the reader entries are pure data callables
        # (`lambda: _read_*(...)`) with no Streamlit writes inside.
        src = self._main_source()
        # The first/second wave list comprehensions are pure data
        # closures -- `st.` (i.e. Streamlit attribute access) only
        # appears outside those list entries.
        for marker, end in (
            ("first_wave_readers: list", "second_wave_readers"),
            ("second_wave_readers: list", "total_reads"),
        ):
            with self.subTest(marker=marker):
                head = src.index(marker)
                tail = src.index(end, head)
                wave_block = src[head:tail]
                self.assertNotIn(" st.", wave_block)
                self.assertNotIn("slot.markdown", wave_block)

    def test_empty_window_short_circuits_second_wave(self) -> None:
        # When the first wave's summary returns no events, the
        # second wave never fires -- the eight remaining widget
        # reads would just paint empty cards. Pin the short-circuit
        # so a future refactor cannot silently re-introduce the
        # wasted reads on an empty window.
        src = self._main_source()
        # The `summary.total_events == 0` check sits between the
        # first-wave dispatch and the second-wave dispatch.
        first = src.index(
            "_fan_out_reads(\n                first_wave_readers"
        )
        second = src.index(
            "_fan_out_reads(\n                    second_wave_readers"
        )
        empty_check = src.index("summary.total_events == 0")
        self.assertLess(first, empty_check)
        self.assertLess(empty_check, second)
        # And the empty branch returns early so the second wave
        # never executes on an empty window.
        empty_block = src[empty_check:second]
        self.assertIn("return", empty_block)


class StagedRenderErrorTest(_MainSourceTest):
    """A read error in EITHER wave must surface as one `st.error` +
    `st.stop` -- the second-wave error path is what stops a half-
    rendered dashboard (topbar / KPI strip already painted) from
    silently continuing into broken widget code.
    """

    def test_both_waves_catch_analytics_read_error(self) -> None:
        src = self._main_source()
        # Each fan-out call must be inside a `try/except
        # analytics_read.AnalyticsReadError` so a worker exception in
        # either wave surfaces as one user-friendly error rather than
        # a stack trace.
        first = src.index(
            "_fan_out_reads(\n                first_wave_readers"
        )
        second = src.index(
            "_fan_out_reads(\n                    second_wave_readers"
        )
        # Walk back from each fan-out call to find the surrounding
        # `try:` -- it must be within a small window (just opens
        # the block) and the matching `except` must catch the read
        # error and stop the dashboard.
        for label, head in (("first", first), ("second", second)):
            with self.subTest(wave=label):
                tail = src.index("st.stop()", head)
                except_idx = src.rindex("except", head, tail)
                handler = src[except_idx:tail + len("st.stop()")]
                self.assertIn(
                    "analytics_read.AnalyticsReadError", handler
                )
                self.assertIn("st.error(", handler)
                self.assertIn("st.stop()", handler)

    def test_second_wave_error_after_topbar_paints(self) -> None:
        # The second-wave error path runs AFTER the topbar / KPI
        # strip have already painted -- so the user sees real
        # content (the topbar + KPI strip) and then a single
        # `st.error` instead of a half-rendered dashboard.
        src = self._main_source()
        topbar_render = src.index("topbar_slot.markdown(")
        second_try = src.index(
            "_fan_out_reads(\n                    second_wave_readers"
        )
        self.assertLess(topbar_render, second_try)


class FanOutReadsErrorPropagationTest(unittest.TestCase):
    """The first-wave error path must NOT swallow the worker's
    `AnalyticsReadError` -- the existing fan-out helper already
    propagates the exception, but the staged-render refactor adds a
    second call site, so re-pin the propagation shape for both
    branches of `_fan_out_reads`.
    """

    def test_sequential_propagates_in_staged_call(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import AnalyticsReadError

        def _boom():
            raise AnalyticsReadError("first wave dead")

        with self.assertRaises(AnalyticsReadError) as cm:
            dashboard._fan_out_reads(
                [("summary", _boom)], parallel=False
            )
        self.assertIn("first wave dead", str(cm.exception))

    def test_parallel_propagates_in_staged_call(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import AnalyticsReadError

        def _boom():
            raise AnalyticsReadError("second wave dead")

        with self.assertRaises(AnalyticsReadError) as cm:
            dashboard._fan_out_reads(
                [("repo_rows", _boom)],
                parallel=True,
                max_workers=2,
            )
        self.assertIn("second wave dead", str(cm.exception))


class FormatTzOffsetTest(unittest.TestCase):
    """`format_tz_offset` renders the integer offset for the sidebar
    label and the heatmap subtitle."""

    def test_zero_is_utc(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.format_tz_offset(0), "UTC")

    def test_positive_offset(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.format_tz_offset(7), "UTC+7")

    def test_negative_offset(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.format_tz_offset(-5), "UTC-5")

    def test_default_offset_is_plus_seven(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.DEFAULT_TZ_OFFSET_HOURS, 7)
        self.assertIn(dashboard.DEFAULT_TZ_OFFSET_HOURS, dashboard.TZ_OFFSET_OPTIONS)


class ShiftTsTest(unittest.TestCase):
    """`shift_ts` converts a UTC `ts` to the wall-clock of the
    selected offset for display in the "Recent agent runs" table."""

    def test_none_passes_through(self) -> None:
        from datetime import timedelta
        _, dashboard = _reload()
        self.assertIsNone(dashboard.shift_ts(None, timedelta(hours=7)))

    def test_aware_ts_converted_to_offset(self) -> None:
        from datetime import timedelta
        _, dashboard = _reload()
        ts = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        shifted = dashboard.shift_ts(ts, timedelta(hours=7))
        self.assertEqual(shifted.hour, 19)
        self.assertEqual(shifted.utcoffset(), timedelta(hours=7))

    def test_aware_ts_negative_offset(self) -> None:
        from datetime import timedelta
        _, dashboard = _reload()
        ts = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        shifted = dashboard.shift_ts(ts, timedelta(hours=-5))
        self.assertEqual(shifted.hour, 7)
        self.assertEqual(shifted.utcoffset(), timedelta(hours=-5))

    def test_naive_ts_shifted_in_place(self) -> None:
        from datetime import timedelta
        _, dashboard = _reload()
        ts = datetime(2026, 6, 5, 12, 0)
        shifted = dashboard.shift_ts(ts, timedelta(hours=7))
        self.assertEqual(shifted, datetime(2026, 6, 5, 19, 0))


if __name__ == "__main__":
    unittest.main()
