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


class ErrorHandlingTest(unittest.TestCase):
    """Connection or query failures wrap in `AnalyticsReadError` so
    callers have a single exception type to catch -- the underlying
    psycopg / driver exception is preserved as `__cause__`.
    """

    def test_connect_failure_wraps(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})

        def _bad_connect(_url: str):
            raise RuntimeError("network unreachable")

        with self.assertRaises(analytics_read.AnalyticsReadError) as ctx:
            analytics_read.get_summary(connect=_bad_connect)
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    def test_query_failure_wraps_and_closes_connection(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.raise_on_execute = RuntimeError("syntax error at or near")
        with self.assertRaises(analytics_read.AnalyticsReadError):
            analytics_read.get_time_series(connect=_connector(conn))
        # `finally` closed the descriptor even though execute raised.
        self.assertEqual(conn.close_called, 1)

    def test_close_failure_is_swallowed(self) -> None:
        # A driver whose `close()` raises after a successful query
        # must not surface that to the dashboard -- the data already
        # came back. `get_data_extent` is the simplest single-query
        # reader to drive this path.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
        t1 = datetime(2026, 5, 28, tzinfo=timezone.utc)
        conn.rows_for = {"data_min_ts": [(t0, t1)]}

        def _bad_close():
            raise RuntimeError("close failed")

        conn.close = _bad_close  # type: ignore[method-assign]
        result = analytics_read.get_data_extent(connect=_connector(conn))
        self.assertEqual(result.min_ts, t0)
        self.assertEqual(result.max_ts, t1)


if __name__ == "__main__":
    unittest.main()
