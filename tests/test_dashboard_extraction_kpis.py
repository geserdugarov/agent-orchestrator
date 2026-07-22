# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard KPI-strip extraction tests."""

import sys


import unittest


from types import MappingProxyType


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


DASHBOARD_KPI_STRIP_MODULE = "orchestrator.dashboard_kpi_strip"


DASHBOARD_WIDGETS_MODULE = "orchestrator.dashboard_widgets"


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


CONFIGURED_DB_URL = "postgresql://h/db"


CONFIGURED_DB_ENV = MappingProxyType({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})


_MOVED_KPI_MEMBERS = (
    "_KpiInputs",
    "_build_kpi_strip_data",
)


class KpiStripExtractionTest(unittest.TestCase):
    """The KPI-strip aggregations -- the token / throughput / rework
    helpers that turn a `Summary` aggregate plus the first-wave read rows
    into the four KPI tiles and the resolved / rejected throughput totals
    -- live in `orchestrator.dashboard_kpi_strip`. `orchestrator.dashboard`
    re-exports the two members the page pipeline and these tests reach
    (`_KpiInputs` / `_build_kpi_strip_data`) under the same names, and
    `dashboard_widgets` imports `_KpiInputs` back from the leaf.
    """

    def test_kpi_members_defined_in_kpi_strip_module(self) -> None:
        _reload(CONFIGURED_DB_ENV)
        kpi_strip = sys.modules[DASHBOARD_KPI_STRIP_MODULE]
        for name in _MOVED_KPI_MEMBERS:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(kpi_strip, name).__module__,
                    DASHBOARD_KPI_STRIP_MODULE,
                )

    def test_facade_reexports_kpi_strip_objects(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        kpi_strip = sys.modules[DASHBOARD_KPI_STRIP_MODULE]
        for name in _MOVED_KPI_MEMBERS:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(dashboard, name),
                    f"dashboard dropped the historical {name!r} alias",
                )
                self.assertIs(getattr(dashboard, name), getattr(kpi_strip, name))

    def test_widgets_imports_kpi_inputs_from_the_leaf(self) -> None:
        _reload(CONFIGURED_DB_ENV)
        widgets = sys.modules[DASHBOARD_WIDGETS_MODULE]
        kpi_strip = sys.modules[DASHBOARD_KPI_STRIP_MODULE]
        self.assertIs(widgets._KpiInputs, kpi_strip._KpiInputs)
