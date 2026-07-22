# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard timestamp-shift tests."""

import unittest


from datetime import datetime, timedelta, timezone


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


_JUN_NOON_UTC_HOUR = 12


_JUN_NOON_NAIVE_HOUR = 12


_TS_CONVERTED_OFFSET_HOUR = 19


_TS_SHIFTED_PLACE_HOUR = 19


_YEAR = 2026


JUN05_NOON_UTC = datetime(_YEAR, 6, 5, _JUN_NOON_UTC_HOUR, 0, tzinfo=timezone.utc)


JUN05_NOON_NAIVE = datetime(_YEAR, 6, 5, _JUN_NOON_NAIVE_HOUR, 0)


class ShiftTsTest(unittest.TestCase):
    """`shift_ts` converts a UTC `ts` to the wall-clock of the
    selected offset for display in the "Recent agent runs" table."""

    def test_none_passes_through(self) -> None:
        _, dashboard = _reload()
        self.assertIsNone(dashboard.shift_ts(None, timedelta(hours=7)))

    def test_aware_ts_converted_to_offset(self) -> None:
        _, dashboard = _reload()
        ts = JUN05_NOON_UTC
        shifted = dashboard.shift_ts(ts, timedelta(hours=7))
        self.assertEqual(shifted.hour, _TS_CONVERTED_OFFSET_HOUR)
        self.assertEqual(shifted.utcoffset(), timedelta(hours=7))

    def test_aware_ts_negative_offset(self) -> None:
        _, dashboard = _reload()
        ts = JUN05_NOON_UTC
        shifted = dashboard.shift_ts(ts, timedelta(hours=-5))
        self.assertEqual(shifted.hour, 7)
        self.assertEqual(shifted.utcoffset(), timedelta(hours=-5))

    def test_naive_ts_shifted_in_place(self) -> None:
        _, dashboard = _reload()
        ts = JUN05_NOON_NAIVE
        shifted = dashboard.shift_ts(ts, timedelta(hours=7))
        self.assertEqual(shifted, JUN05_NOON_NAIVE.replace(hour=_TS_SHIFTED_PLACE_HOUR))
