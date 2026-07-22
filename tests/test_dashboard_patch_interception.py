# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard facade patch-interception tests."""

import unittest


from types import MappingProxyType, SimpleNamespace


from unittest.mock import patch

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


RUN_READ_WAVES_MEMBER = "_run_read_waves"


class _NullContext:
    """`with`-usable stand-in for `st.container(...)` / `st.columns(...)`."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingStreamlit:
    """Minimal fake `st` that records the `config=` handed to `plotly_chart`.

    Only the surface `_render_activity_heatmap` touches is implemented; every
    other call is a no-op so the render runs without the optional Streamlit.
    """

    def __init__(self) -> None:
        self.plotly_configs: list = []

    def container(self, **kwargs):
        return _NullContext()

    def markdown(self, *args, **kwargs) -> None:
        """No-op stand-in for `st.markdown`."""

    def selectbox(self, *args, **kwargs) -> None:
        """No-op stand-in for `st.selectbox`."""

    def plotly_chart(self, figure, *, config=None, **kwargs) -> None:
        self.plotly_configs.append(config)


class _StubCharts:
    def hour_weekday_heatmap(self, rows, *, tz_label):
        return object()


class FacadePatchInterceptionTest(unittest.TestCase):
    """The moved page-pipeline resolves its siblings, the read-wave dispatch,
    and `PLOTLY_CONFIG` through the `orchestrator.dashboard` facade at call
    time, so `patch.object(dashboard, ...)` intercepts the running pipeline
    (mirroring the workflow.py stage-handler facade). Identity re-export alone
    would not catch a stubbed rebind, so these drive the callers under a patch.
    """

    def test_patched_run_read_waves_drives_data_load(self) -> None:
        # `_load_dashboard_data` reaches the read-wave dispatch through the
        # facade, so a patched `dashboard._run_read_waves` supplies the data.
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        read_results = {"summary": object()}
        kpis = object()
        with patch.object(
            dashboard,
            RUN_READ_WAVES_MEMBER,
            return_value=(read_results, kpis),
        ) as stub:
            loaded = dashboard._load_dashboard_data(
                SimpleNamespace(st=object()),
                SimpleNamespace(reads=object()),
            )
            stub.assert_called_once()
        self.assertIs(loaded.read_results, read_results)
        self.assertIs(loaded.kpis, kpis)

    def test_patched_sections_drive_page_render(self) -> None:
        # `_render_dashboard_widgets` reaches both wave renderers through the
        # facade, so patched stubs run in place of the real sections.
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        calls: list[str] = []
        with (
            patch.object(
                dashboard,
                "_render_chart_widgets",
                side_effect=lambda *args: calls.append("chart"),
            ),
            patch.object(
                dashboard,
                "_render_remaining_widgets",
                side_effect=lambda *args: calls.append("remaining"),
            ),
        ):
            dashboard._render_dashboard_widgets(object(), object(), object())
        self.assertEqual(calls, ["chart", "remaining"])

    def test_patched_plotly_config_reaches_chart(self) -> None:
        # A leaf renderer reads `PLOTLY_CONFIG` through the facade and hands
        # Plotly a plain-dict copy, so patching `dashboard.PLOTLY_CONFIG`
        # changes the config the chart receives (never the mapping proxy,
        # which is not JSON-serializable).
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        fake_st = _RecordingStreamlit()
        sentinel = {"displayModeBar": True, "scrollZoom": True}
        with patch.object(dashboard, "PLOTLY_CONFIG", sentinel):
            dashboard._render_activity_heatmap(
                st=fake_st,
                dashboard_charts=_StubCharts(),
                heatmap_rows=[],
                tz_offset_choice=0,
            )
        self.assertEqual(fake_st.plotly_configs, [sentinel])
        self.assertIsInstance(fake_st.plotly_configs[0], dict)
        self.assertIsNot(fake_st.plotly_configs[0], sentinel)
