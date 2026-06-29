# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tests.analytics_read_helpers import (
    _FakeConnection,
    _connector,
    _reload,
)


class DataExtentTest(unittest.TestCase):
    """`get_data_extent` answers "what date range does the data
    actually cover" so the sidebar date picker can default to a
    window that contains rows. Empty / unset cases yield the
    zero-valued `DataExtent` so the dashboard can branch on it."""

    def test_unset_db_url_returns_empty_extent(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        connected = []
        result = analytics_read.get_data_extent(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result, analytics_read.DataExtent())

    def test_empty_table_returns_null_extents(self) -> None:
        # Postgres' MIN/MAX on an empty table returns one row of two
        # NULLs; the read model surfaces that as `(None, None)`.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"data_min_ts": [(None, None)]}
        result = analytics_read.get_data_extent(connect=_connector(conn))
        self.assertIsNone(result.min_ts)
        self.assertIsNone(result.max_ts)

    def test_returns_min_and_max(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        t0 = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        conn.rows_for = {"data_min_ts": [(t0, t1)]}
        result = analytics_read.get_data_extent(connect=_connector(conn))
        self.assertEqual(result.min_ts, t0)
        self.assertEqual(result.max_ts, t1)
        sql, _ = conn.executed[0]
        self.assertIn("MIN(ts)", sql)
        self.assertIn("MAX(ts)", sql)


if __name__ == "__main__":
    unittest.main()
