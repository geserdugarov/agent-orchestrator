# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

_EXTENT_MAX_DAY = 28

# Reused data-extent bounds; the year is pinned so the fixture
# timestamps stay stable, the day components are incidental.
_YEAR = 2026
_EXTENT_MIN = datetime(_YEAR, 5, 1, tzinfo=timezone.utc)
_EXTENT_MAX = datetime(_YEAR, 5, _EXTENT_MAX_DAY, tzinfo=timezone.utc)


def _raise_network_error(_url: str) -> None:
    """Stand in for a `connect` that never reaches the database."""
    raise RuntimeError("network unreachable")


def _raise_on_close() -> None:
    """Stand in for a driver whose `close()` fails after the query."""
    raise RuntimeError("close failed")


class ErrorHandlingTest(unittest.TestCase):
    """Connection or query failures wrap in `AnalyticsReadError` so
    callers have a single exception type to catch -- the underlying
    psycopg / driver exception is preserved as `__cause__`.
    """

    def test_connect_failure_wraps(self) -> None:
        analytics_read = _reload_read()
        try:
            analytics_read.get_summary(connect=_raise_network_error)
        except analytics_read.AnalyticsReadError as read_error:
            self.assertIsInstance(read_error.__cause__, RuntimeError)
        else:
            self.fail("expected AnalyticsReadError")

    def test_query_failure_wraps_and_closes_conn(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.raise_on_execute = RuntimeError("syntax error at or near")
        with self.assertRaises(analytics_read.AnalyticsReadError):
            analytics_read.get_time_series(connect=conn.as_connect)
        # `finally` closed the descriptor even though execute raised.
        self.assertEqual(conn.close_called, 1)

    def test_reused_conn_failure_wraps_without_close(self) -> None:
        # The `conn=` reuse path wraps a driver error the same way the
        # open-per-call path does, but must NOT close the caller-owned
        # connection -- its lifetime belongs to the `analytics_connection`
        # scope, not to this single query.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.raise_on_execute = RuntimeError("syntax error at or near")
        with self.assertRaises(analytics_read.AnalyticsReadError):
            analytics_read.get_time_series(conn=conn)
        self.assertEqual(conn.close_called, 0)

    def test_close_failure_is_swallowed(self) -> None:
        # A driver whose `close()` raises after a successful query
        # must not surface that to the dashboard -- the data already
        # came back. `get_data_extent` is the simplest single-query
        # reader to drive this path.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {"data_min_ts": [(_EXTENT_MIN, _EXTENT_MAX)]}
        conn.close = _raise_on_close  # type: ignore[method-assign]
        extent = analytics_read.get_data_extent(connect=conn.as_connect)
        self.assertEqual(extent.min_ts, _EXTENT_MIN)
        self.assertEqual(extent.max_ts, _EXTENT_MAX)


if __name__ == "__main__":
    unittest.main()
