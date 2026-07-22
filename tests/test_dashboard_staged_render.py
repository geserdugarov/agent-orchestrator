# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard staged-render ordering and error propagation tests."""

import inspect


import unittest


from functools import partial


from types import MappingProxyType


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
    load_analytics_read as _analytics_read_module,
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


FIRST_WAVE_READER_NAMES = (
    "summary",
    "prev_summary",
    "ts_points",
    "review_round_rows",
    "throughput_rows",
    "cost_coverage_rows",
)


SECOND_WAVE_READER_NAMES = (
    "stage_rows",
    "agent_exits",
    "issues_rows",
    "backend_rows",
    "repo_rows",
    "heatmap_rows",
    "backend_daily_rows",
    "skill_adoption_rows",
    "skill_rows",
    "skill_matrix_rows",
)


ENTRYPOINT_ATTR = "main"


FIRST_WAVE_READERS_MEMBER = "_first_wave_readers"


SECOND_WAVE_READERS_MEMBER = "_second_wave_readers"


RUN_READ_WAVES_MEMBER = "_run_read_waves"


RENDER_FIRST_WAVE_MEMBER = "_render_first_wave"


DISPATCH_FIRST_WAVE = "reads.first_wave"


DISPATCH_SECOND_WAVE = "reads.second_wave"


def _raise_read_error(
    message: str,
    calls: list[str] | None = None,
    call_name: str | None = None,
) -> None:
    read_error = _analytics_read_module().AnalyticsReadError
    if calls is None or call_name is None:
        raise read_error(message)
    calls.append(call_name)
    raise read_error(message)


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

    def _source_between(self, source: str, start: str, end: str) -> str:
        return source[source.index(start):source.index(end)]

    def _source_tail(self, source: str, start: str) -> str:
        return source[source.index(start):]


class _StagedRenderSupport(_MainSourceTest):
    """The two read waves preserve their inputs and progressive render order."""

    def _first_wave_source(self) -> str:
        return self._source_of(FIRST_WAVE_READERS_MEMBER)

    def _second_wave_source(self) -> str:
        return self._source_of(SECOND_WAVE_READERS_MEMBER)

    def _load_source(self) -> str:
        return self._source_of(RUN_READ_WAVES_MEMBER)


class StagedRenderWaveTest(_StagedRenderSupport):
    def test_first_wave_has_only_topbar_inputs(self) -> None:
        wave = self._first_wave_source()
        for name in FIRST_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', wave)
        for name in SECOND_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(f'"{name}"', wave)

    def test_second_wave_has_remaining_reads(self) -> None:
        wave = self._second_wave_source()
        for name in SECOND_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', wave)
        for name in FIRST_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(f'"{name}"', wave)

    def test_topbar_and_meta_render_between_waves(self) -> None:
        self._assert_source_order(
            RUN_READ_WAVES_MEMBER,
            (DISPATCH_FIRST_WAVE, "render_first_wave(", DISPATCH_SECOND_WAVE),
        )

        first_render_source = self._source_of(RENDER_FIRST_WAVE_MEMBER)
        self.assertIn("_render_topbar_and_meta(", first_render_source)
        self.assertIn("_kpi_strip_html(", first_render_source)
        topbar_source = self._source_of("_render_topbar_and_meta")
        self.assertIn("topbar_slot.markdown(", topbar_source)
        self.assertIn("meta_slot.markdown(", topbar_source)


class StagedRenderRuntimeTest(_StagedRenderSupport):
    def test_inline_loading_spinner_wraps_fan_out(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertEqual(dashboard.LOADING_INDICATOR_MESSAGE, "Loading analytics…")
        source = self._load_source()
        spinner = source.index("with st.spinner(LOADING_INDICATOR_MESSAGE):")
        first = source.index(DISPATCH_FIRST_WAVE)
        second = source.index(DISPATCH_SECOND_WAVE)
        self.assertLess(spinner, first)
        self.assertLess(spinner, second)

    def test_widgets_render_on_main_thread(self) -> None:
        for helper in (FIRST_WAVE_READERS_MEMBER, SECOND_WAVE_READERS_MEMBER):
            with self.subTest(helper=helper):
                wave_source = self._source_of(helper)
                self.assertIn("_widget_task(", wave_source)
                self.assertNotIn(".markdown(", wave_source)
                self.assertNotIn(".info(", wave_source)
                self.assertNotIn(".plotly_chart(", wave_source)
        task_source = self._source_of("_widget_task")
        self.assertIn("partial(cached_reader, *args)", task_source)
        self.assertNotIn(".markdown(", task_source)

    def test_empty_window_short_circuits_second_wave(self) -> None:
        load_source = self._load_source()
        short_circuit = self._source_between(
            load_source,
            "render_first_wave(",
            DISPATCH_SECOND_WAVE,
        )
        self.assertIn("if first_wave is None:", short_circuit)
        self.assertIn("return None", short_circuit)

        first_wave_source = self._source_of(RENDER_FIRST_WAVE_MEMBER)
        self._assert_source_order(
            RENDER_FIRST_WAVE_MEMBER,
            ("summary.total_events == 0", "_render_empty_window("),
        )
        self.assertIn(
            "return None",
            self._source_tail(first_wave_source, "summary.total_events == 0"),
        )


class StagedRenderErrorTest(_MainSourceTest):
    """Both read waves share error handling and retain their render order."""

    def test_both_waves_route_through_dispatch_helper(self) -> None:
        source = self._source_of(RUN_READ_WAVES_MEMBER)
        self.assertIn(DISPATCH_FIRST_WAVE, source)
        self.assertIn(DISPATCH_SECOND_WAVE, source)
        self.assertEqual(source.count("_dispatch_reads("), 2)

    def test_second_wave_error_after_topbar_paints(self) -> None:
        source = self._source_of(RUN_READ_WAVES_MEMBER)
        first_wave_render = source.index("render_first_wave(")
        second_dispatch = source.index(DISPATCH_SECOND_WAVE)
        self.assertLess(first_wave_render, second_dispatch)
        self.assertIn(
            "_render_topbar_and_meta(",
            self._source_of(RENDER_FIRST_WAVE_MEMBER),
        )


class FanOutReadsErrorPropagationTest(unittest.TestCase):
    """The first-wave error path must NOT swallow the worker's
    `AnalyticsReadError` -- the existing fan-out helper already
    propagates the exception, but the staged-render refactor adds a
    second call site, so re-pin the propagation shape for both
    branches of `_fan_out_reads`.
    """

    def test_sequential_propagates_in_staged_call(self) -> None:
        _, dashboard = _reload()
        read_error = _analytics_read_module().AnalyticsReadError

        with self.assertRaisesRegex(read_error, "first wave dead"):
            dashboard._fan_out_reads(
                [("summary", partial(_raise_read_error, "first wave dead"))],
                parallel=False,
            )

    def test_parallel_propagates_in_staged_call(self) -> None:
        _, dashboard = _reload()
        read_error = _analytics_read_module().AnalyticsReadError

        with self.assertRaisesRegex(read_error, "second wave dead"):
            dashboard._fan_out_reads(
                [("repo_rows", partial(_raise_read_error, "second wave dead"))],
                parallel=True,
                max_workers=2,
            )
