# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard current and previous window tests."""

import unittest


from datetime import date, datetime, timezone


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_MAY_DAY = 15


_MAY_DAY_SECONDARY = 22


_MAY_DAY_TERTIARY = 28


_YEAR = 2026


MAY01 = date(_YEAR, 5, 1)


MAY02 = date(_YEAR, 5, 2)


MAY03 = date(_YEAR, 5, 3)


MAY04 = date(_YEAR, 5, 4)


MAY05 = date(_YEAR, 5, 5)


MAY06 = date(_YEAR, 5, 6)


MAY07 = date(_YEAR, 5, 7)


MAY15 = date(_YEAR, 5, _MAY_DAY)


MAY22 = date(_YEAR, 5, _MAY_DAY_SECONDARY)


MAY28 = date(_YEAR, 5, _MAY_DAY_TERTIARY)


def _utc_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


class ToWindowTest(unittest.TestCase):
    def test_inclusive_end_becomes_exclusive_midnight(self) -> None:
        # `analytics_read` uses `ts < end`; midnight on the day after
        # `end_date` is what makes events from `end_date` visible.
        _, dashboard = _reload()
        window = dashboard.to_window(MAY01, MAY03)
        self.assertEqual(window.start, _utc_midnight(MAY01))
        self.assertEqual(window.end, _utc_midnight(MAY04))

    def test_reversed_range_is_swapped(self) -> None:
        # The Streamlit two-date input lets the user type end < start.
        # Swapping silently keeps the dashboard useful instead of
        # collapsing to an empty SQL window.
        _, dashboard = _reload()
        window = dashboard.to_window(MAY05, MAY01)
        self.assertEqual(window.start.date(), MAY01)
        self.assertEqual(window.end.date(), MAY06)

    def test_single_day_window(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(MAY01, MAY01)
        self.assertEqual(window.start, _utc_midnight(MAY01))
        self.assertEqual(window.end, _utc_midnight(MAY02))


class PreviousWindowTest(unittest.TestCase):
    """The previous-window helper feeds the KPI delta column. It must
    return a window of the same length immediately before `window`
    so the deltas compare like-for-like (e.g. last-30-days vs the
    30 days before that).
    """

    def test_length_preserved(self) -> None:
        _, dashboard = _reload()
        win = dashboard.to_window(MAY01, MAY07)
        prev = dashboard.previous_window(win)
        self.assertEqual(prev.end, win.start)
        self.assertEqual(prev.end - prev.start, win.end - win.start)

    def test_seven_day_window_has_seven_day_prior(self) -> None:
        _, dashboard = _reload()
        win = dashboard.to_window(MAY22, MAY28)
        prev = dashboard.previous_window(win)
        # `to_window`'s end is exclusive (one day past `end_date`),
        # so the seven-day window spans 7 calendar days; the previous
        # window starts seven days before the current start.
        self.assertEqual(prev.start.date(), MAY15)
        self.assertEqual(prev.end.date(), MAY22)
