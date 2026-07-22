# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard widget extraction tests."""

import sys


import unittest


from types import MappingProxyType


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


DASHBOARD_WIDGETS_MODULE = "orchestrator.dashboard_widgets"


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


CONFIGURED_DB_URL = "postgresql://h/db"


CONFIGURED_DB_ENV = MappingProxyType({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})


_MOVED_WIDGET_MEMBERS = (
    "_DashboardModules",
    "_DashboardFilters",
    "_DashboardControls",
    "_DashboardPage",
    "_backend_tokens_by_day",
    "_load_dashboard_data",
    "_render_topbar_and_meta",
    "_render_first_wave",
    "_render_chart_widgets",
    "_render_remaining_widgets",
    "_render_dashboard_widgets",
    "_render_dashboard_footer",
    "_render_no_data",
    "_render_empty_window",
    "_render_hero_usage",
    "_render_stage_review_bars",
    "_render_issues_and_backends",
    "_render_repo_and_reliability",
    "_render_activity_heatmap",
    "_render_skill_adoption",
    "_render_skill_invocation_diagnostics",
    "_render_skill_triggers",
    "_render_skill_matrix_expander",
    "_render_recent_runs",
    "_render_drilldown_view",
)


_WIDGETS_FACADE_CONSTANTS = (
    "PLOTLY_CONFIG",
    "NO_DATA_MESSAGE",
    "EMPTY_WINDOW_MESSAGE",
)


class WidgetRenderingExtractionTest(unittest.TestCase):
    """The widget-rendering pipeline -- the two-wave render passes, the
    empty / no-data states, the per-issue drill-down renderer, the page
    footer, and the page-state dataclasses the pipeline threads -- lives in
    `orchestrator.dashboard_widgets`, and `orchestrator.dashboard`
    re-exports the members the page pipeline and these tests reach under
    the same names so the `dashboard.<name>` surface keeps resolving to the
    same object. The KPI-strip aggregations live in
    `orchestrator.dashboard_kpi_strip` (`KpiStripExtractionTest`).
    """

    def test_widget_members_defined_in_widgets_module(self) -> None:
        _reload(CONFIGURED_DB_ENV)
        widgets = sys.modules[DASHBOARD_WIDGETS_MODULE]
        for name in _MOVED_WIDGET_MEMBERS:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(widgets, name).__module__,
                    DASHBOARD_WIDGETS_MODULE,
                )

    def test_facade_reexports_widgets_objects(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        widgets = sys.modules[DASHBOARD_WIDGETS_MODULE]
        for name in (*_MOVED_WIDGET_MEMBERS, *_WIDGETS_FACADE_CONSTANTS):
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(dashboard, name),
                    f"dashboard dropped the historical {name!r} alias",
                )
                self.assertIs(getattr(dashboard, name), getattr(widgets, name))
