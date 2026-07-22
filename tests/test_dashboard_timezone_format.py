# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard timezone-label tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


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
