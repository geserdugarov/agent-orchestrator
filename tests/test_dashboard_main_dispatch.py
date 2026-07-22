# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard main-render dispatch and parallel fan-out wiring tests."""

import inspect


import unittest


from types import MappingProxyType


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


SKIP_DOTENV_ENV = "ORCHESTRATOR_SKIP_DOTENV"


TOKEN_FILE_ENV = "ORCHESTRATOR_TOKEN_FILE"


MISSING_TOKEN_FILE = "/tmp/agent-orchestrator-token-missing"


ANALYTICS_READ_MODULE = "orchestrator.analytics.read"


DASHBOARD_MODULE = "orchestrator.dashboard"


DASHBOARD_CARDS_MODULE = "orchestrator.dashboard_cards"


DASHBOARD_KPI_STRIP_MODULE = "orchestrator.dashboard_kpi_strip"


DASHBOARD_READS_MODULE = "orchestrator.dashboard_reads"


DASHBOARD_WIDGETS_MODULE = "orchestrator.dashboard_widgets"


DASHBOARD_STATE_MODULE = "orchestrator.dashboard_state"


_RELOAD_POP_MODULES = (
    "orchestrator.config",
    ANALYTICS_READ_MODULE,
    "orchestrator.analytics",
    DASHBOARD_STATE_MODULE,
    "orchestrator.dashboard_kpis",
    "orchestrator.dashboard_html",
    DASHBOARD_CARDS_MODULE,
    DASHBOARD_KPI_STRIP_MODULE,
    "orchestrator.dashboard_skill_adoption",
    "orchestrator.dashboard_skill_matrix",
    DASHBOARD_READS_MODULE,
    DASHBOARD_WIDGETS_MODULE,
    DASHBOARD_MODULE,
)


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


CONFIGURED_DB_URL = "postgresql://h/db"


CONFIGURED_DB_ENV = MappingProxyType({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})


ENTRYPOINT_ATTR = "main"


RUN_READ_WAVES_MEMBER = "_run_read_waves"


RENDER_FIRST_WAVE_MEMBER = "_render_first_wave"


class _MainSourceTest(unittest.TestCase):
    """Base for source checks over the lazy entrypoint and page helpers.

    Streamlit / Plotly are opt-in (not installed for the default
    `uv sync --locked`), so these read the rendered function source
    rather than driving the page under Streamlit. The entrypoint loads
    optional modules lazily and the page pipeline delegates controls,
    read waves, empty states, and widget sections to named helpers, so
    `_source_of` fetches the boundary each assertion protects.
    """

    def _main_source(self) -> str:
        return self._source_of(ENTRYPOINT_ATTR)

    def _source_of(self, name: str) -> str:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        return inspect.getsource(getattr(dashboard, name))

    def _assert_source_order(self, member_name: str, markers: tuple[str, ...]) -> None:
        source = self._source_of(member_name)
        indexes = [source.index(marker) for marker in markers]
        self.assertEqual(indexes, sorted(indexes))


class MainRenderDispatchTest(_MainSourceTest):
    """The page pipeline preserves control and widget render order."""

    def test_render_helpers_called_in_page_order(self) -> None:
        self._assert_source_order(
            "_render_dashboard_controls",
            ("_render_sidebar_filters(", "_render_date_filter_bar("),
        )
        self._assert_source_order(
            "_render_chart_widgets",
            (
                "_render_hero_usage(",
                "_render_stage_review_bars(",
                "_render_issues_and_backends(",
                "_render_repo_and_reliability(",
                "_render_activity_heatmap(",
            ),
        )
        self._assert_source_order(
            "_render_remaining_widgets",
            (
                "_render_skill_adoption(",
                "_render_recent_runs(",
                "_render_drilldown_view(",
                "_render_dashboard_footer(",
            ),
        )
        self._assert_source_order(
            "_render_dashboard_widgets",
            ("_render_chart_widgets(", "_render_remaining_widgets("),
        )

    def test_read_and_error_paths_use_helpers(self) -> None:
        # `main` dispatches the staged read fan-out and the empty /
        # error rendering branches through focused helpers rather than
        # inlining the cached wrappers, the fan-out, the load log, and
        # the metadata / no-data / empty-window banners.
        for helper, marker in (
            ("_run_dashboard", "_read_static_metadata("),
            ("_render_dashboard", "_render_no_data("),
            ("_prepare_dashboard_page", "_widget_readers("),
            ("_load_dashboard_data", "_run_read_waves("),
            (RUN_READ_WAVES_MEMBER, "_dispatch_reads("),
            (RUN_READ_WAVES_MEMBER, "_log_dashboard_load("),
            (RENDER_FIRST_WAVE_MEMBER, "_render_empty_window("),
        ):
            with self.subTest(helper=helper, marker=marker):
                self.assertIn(marker, self._source_of(helper))
        # The read-error banners and the cached wrappers belong to the
        # helpers, so `main` never inlines `st.error(`, a
        # `_fan_out_reads` call, or a `_read_*` wrapper definition.
        main_src = self._main_source()
        self.assertNotIn("st.error(", main_src)
        self.assertNotIn("_fan_out_reads(", main_src)
        self.assertNotIn("def _read_summary(", main_src)


class MainParallelFanOutWiringTest(_MainSourceTest):
    """`main()` dispatches the widget reads through `_dispatch_reads`
    (which wraps `_fan_out_reads`), drives the parallel switch off the
    env-backed helper, and logs a single `dashboard.load:` INFO line
    via `_log_dashboard_load` so the A/B rollout has a measurement
    surface. Streamlit is not installed for the default `uv sync
    --locked`, so these inspect the rendered sources rather than
    driving the page under Streamlit.
    """

    def test_dispatch_wraps_fan_out_helper(self) -> None:
        # `_fan_out_reads` is the single dispatch surface; the data load
        # reaches it through `_run_read_waves` -> `_dispatch_reads`, and
        # `_load_dashboard_data` delegates the whole two-wave run to
        # `_run_read_waves`.
        self.assertIn(
            "_run_read_waves(",
            self._source_of("_load_dashboard_data"),
        )
        self.assertIn(
            "_dispatch_reads(",
            self._source_of(RUN_READ_WAVES_MEMBER),
        )
        self.assertIn(
            "_fan_out_reads(",
            self._source_of("_dispatch_reads"),
        )

    def test_main_drives_parallel_off_env_helper(self) -> None:
        src = self._source_of("_prepare_dashboard_page")
        # The env-backed helper is the single source of truth for the
        # flag so a test or shutdown hook can flip it without
        # rewriting `main()`.
        self.assertIn("dashboard_parallel_reads_enabled()", src)

    def test_main_emits_load_timing_log(self) -> None:
        # The instrumentation line carries total wall-clock, reader
        # count, and the parallel flag so the operator can A/B with a
        # single grep; it is emitted by `_log_dashboard_load`, which the
        # `_run_read_waves` data load calls (clocked from the
        # `perf_counter` stamped in `_prepare_dashboard_page`).
        load_src = self._source_of(RUN_READ_WAVES_MEMBER)
        prepare_src = self._source_of("_prepare_dashboard_page")
        self.assertIn("_log_dashboard_load(", load_src)
        self.assertIn("perf_counter()", prepare_src)
        self.assertIn(
            "dashboard.load:",
            self._source_of("_log_dashboard_load"),
        )

    def test_dispatch_catches_analytics_read_error(self) -> None:
        # The `_dispatch_reads` wave helper wraps the fan-out in a
        # `try/except AnalyticsReadError` -> one `st.error` + stop, so
        # a worker exception surfaces as one banner rather than a
        # trace. (Both waves route through this single helper; the
        # routing is pinned by `StagedRenderErrorTest`.)
        dispatch_src = self._source_of("_dispatch_reads")
        self.assertIn("analytics_read.AnalyticsReadError", dispatch_src)
        self.assertIn("st.error(", dispatch_src)
        self.assertIn("st.stop()", dispatch_src)
