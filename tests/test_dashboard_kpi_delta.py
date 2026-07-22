# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard KPI delta tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_POSITIVE_DELTA_KPI_DELTA_ARG = 125


_POSITIVE_DELTA_KPI_DELTA = 0.25


_NEGATIVE_DELTA_KPI_DELTA_ARG = 75


_NEGATIVE_DELTA_KPI_DELTA = 0.25


class KpiDeltaTest(unittest.TestCase):
    def test_positive_delta(self) -> None:
        _, dashboard = _reload()
        self.assertAlmostEqual(dashboard.kpi_delta(_POSITIVE_DELTA_KPI_DELTA_ARG, 100), _POSITIVE_DELTA_KPI_DELTA)

    def test_negative_delta(self) -> None:
        _, dashboard = _reload()
        self.assertAlmostEqual(dashboard.kpi_delta(_NEGATIVE_DELTA_KPI_DELTA_ARG, 100), -_NEGATIVE_DELTA_KPI_DELTA)

    def test_zero_previous_returns_none(self) -> None:
        # The dashboard hides the delta indicator rather than
        # rendering an infinity for the zero-baseline case.
        _, dashboard = _reload()
        self.assertIsNone(dashboard.kpi_delta(10, 0))

    def test_negative_previous_returns_none(self) -> None:
        _, dashboard = _reload()
        self.assertIsNone(dashboard.kpi_delta(10, -5))
