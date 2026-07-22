# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

_EXTENT_MAX_DAY = 27
_EXTENT_MAX_HOUR = 12

# Earliest / latest timestamps the populated-extent fixture reports;
# the year is pinned so the bounds stay stable across runs.
_YEAR = 2026
_EXTENT_MIN = datetime(_YEAR, 4, 1, tzinfo=timezone.utc)
_EXTENT_MAX = datetime(_YEAR, 5, _EXTENT_MAX_DAY, _EXTENT_MAX_HOUR, 0, tzinfo=timezone.utc)


class DataExtentTest(unittest.TestCase):
    """`get_data_extent` answers "what date range does the data
    actually cover" so the sidebar date picker can default to a
    window that contains rows. Empty / unset cases yield the
    zero-valued `DataExtent` so the dashboard can branch on it."""

    def test_unset_db_url_returns_empty_extent(self) -> None:
        analytics_read = _reload_read(db_url="")
        connected = []
        extent = analytics_read.get_data_extent(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(extent, analytics_read.DataExtent())

    def test_empty_table_returns_null_extents(self) -> None:
        # Postgres' MIN/MAX on an empty table returns one row of two
        # NULLs; the read model surfaces that as `(None, None)`.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {"data_min_ts": [(None, None)]}
        extent = analytics_read.get_data_extent(connect=conn.as_connect)
        self.assertIsNone(extent.min_ts)
        self.assertIsNone(extent.max_ts)

    def test_returns_min_and_max(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {"data_min_ts": [(_EXTENT_MIN, _EXTENT_MAX)]}
        extent = analytics_read.get_data_extent(connect=conn.as_connect)
        self.assertEqual(extent.min_ts, _EXTENT_MIN)
        self.assertEqual(extent.max_ts, _EXTENT_MAX)
        sql, _ = conn.first_query
        self.assertIn("MIN(ts)", sql)
        self.assertIn("MAX(ts)", sql)


if __name__ == "__main__":
    unittest.main()
