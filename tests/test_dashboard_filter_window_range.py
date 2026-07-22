# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard preset-window range tests."""

import unittest


from datetime import date, datetime, timezone


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_MAY_DAY = 22


_MAY_DAY_SECONDARY = 26


_MAY_DAY_TERTIARY = 28


_MAY_DAY_QUATERNARY = 29


_EXTENT_MAX_TS = 23


_EXTENT_MAX_TS_SECONDARY = 59


_YEAR = 2026


MAY01 = date(_YEAR, 5, 1)


MAY22 = date(_YEAR, 5, _MAY_DAY)


MAY26 = date(_YEAR, 5, _MAY_DAY_SECONDARY)


MAY28 = date(_YEAR, 5, _MAY_DAY_TERTIARY)


MAY29 = date(_YEAR, 5, _MAY_DAY_QUATERNARY)


JAN01 = date(_YEAR, 1, 1)


class _PresetWindowSupport(unittest.TestCase):
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
            min_ts=datetime(min_d.year, min_d.month, min_d.day, tzinfo=timezone.utc),
            max_ts=datetime(
                max_d.year, max_d.month, max_d.day, _EXTENT_MAX_TS, _EXTENT_MAX_TS_SECONDARY, tzinfo=timezone.utc
            ),
        )


class PresetWindowRangeTest(_PresetWindowSupport):
    def test_three_day_preset_anchors_at_max(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(MAY01, MAY28)
        window = dashboard.preset_window(dashboard.PRESET_3D, extent)
        self.assertIsNotNone(window)
        # Three-day preset spans the max date and the two days before
        # it, exclusive end at midnight the day after the max.
        self.assertEqual(window.start.date(), MAY26)
        self.assertEqual(window.end.date(), MAY29)

    def test_seven_day_preset_anchors_at_max(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(MAY01, MAY28)
        window = dashboard.preset_window(dashboard.PRESET_7D, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), MAY22)
        self.assertEqual(window.end.date(), MAY29)

    def test_seven_day_preset_clamps_to_min(self) -> None:
        # Data extent is only 3 days wide -- "Last 7 days" must
        # clamp the start at the data extent's min, not reach
        # before it.
        _, dashboard = _reload()
        extent = self._extent(MAY26, MAY28)
        window = dashboard.preset_window(dashboard.PRESET_7D, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), MAY26)
        self.assertEqual(window.end.date(), MAY29)

    def test_all_preset_covers_full_extent(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(JAN01, MAY28)
        window = dashboard.preset_window(dashboard.PRESET_ALL, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), JAN01)
        self.assertEqual(window.end.date(), MAY29)
