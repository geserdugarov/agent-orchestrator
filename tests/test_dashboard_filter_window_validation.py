# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard preset-window validation tests."""

import unittest


from datetime import date, datetime, timezone


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_MAY_DAY_TERTIARY = 28


_EXTENT_MAX_TS = 23


_EXTENT_MAX_TS_SECONDARY = 59


_YEAR = 2026


MAY01 = date(_YEAR, 5, 1)


MAY28 = date(_YEAR, 5, _MAY_DAY_TERTIARY)


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


class PresetWindowValidationTest(_PresetWindowSupport):
    def test_custom_preset_returns_none(self) -> None:
        # The caller renders a date-range picker when the preset is
        # `Custom`; `preset_window` returns `None` so the caller can
        # branch on a falsy value rather than special-casing the
        # preset string in two places.
        _, dashboard = _reload()
        extent = self._extent(MAY01, MAY28)
        self.assertIsNone(dashboard.preset_window(dashboard.PRESET_CUSTOM, extent))

    def test_empty_extent_returns_none(self) -> None:
        _, dashboard = _reload()
        empty = dashboard.DataExtent()
        self.assertIsNone(dashboard.preset_window(dashboard.PRESET_7D, empty))

    def test_unknown_preset_returns_none(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(MAY01, MAY28)
        self.assertIsNone(dashboard.preset_window("not-a-preset", extent))

    def test_preset_options_match_redesign(self) -> None:
        # Pin the inline labels the topbar exposes (3D / 7D / All)
        # and the full option tuple including the Custom fallback so
        # a future refactor cannot silently re-introduce the old
        # `30d` preset.
        _, dashboard = _reload()
        self.assertEqual(
            dashboard.PRESET_OPTIONS,
            (dashboard.PRESET_3D, dashboard.PRESET_7D, dashboard.PRESET_ALL, dashboard.PRESET_CUSTOM),
        )
        self.assertEqual(
            set(dashboard.PRESET_INLINE_LABELS),
            {dashboard.PRESET_3D, dashboard.PRESET_7D, dashboard.PRESET_ALL},
        )
