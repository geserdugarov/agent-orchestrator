# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard default date-range tests."""

import unittest


from datetime import date


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_MAY_DAY_SECONDARY = 22


_MAY_DAY_TERTIARY = 28


_YEAR = 2026


MAY22 = date(_YEAR, 5, _MAY_DAY_SECONDARY)


MAY28 = date(_YEAR, 5, _MAY_DAY_TERTIARY)


class DefaultDateRangeTest(unittest.TestCase):
    def test_window_includes_today_and_n_days(self) -> None:
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(today=MAY28, days=7)
        self.assertEqual(end, MAY28)
        self.assertEqual(start, MAY22)

    def test_days_one_yields_today_only(self) -> None:
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(today=MAY28, days=1)
        self.assertEqual(start, end)

    def test_days_zero_clamps_to_today_only(self) -> None:
        # `days=0` is non-sensical (an empty window) so the helper
        # clamps to "today only" instead of returning end < start.
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(today=MAY28, days=0)
        self.assertEqual(start, MAY28)
        self.assertEqual(end, MAY28)
