# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

# The summary CTE the combined query opens with, plus an all-zero
# totals row that keeps the reader from short-circuiting when the
# values themselves are irrelevant to the reuse assertions.
_WIN_CTE = "WITH win AS"
_ZERO_TOTALS_ROW = ("t", None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

# Non-null data-extent bounds for the `get_data_extent` fakes; the
# reuse tests only assert they round-trip as non-None.
_YEAR = 2026
_WINDOW_START = datetime(_YEAR, 5, 1, tzinfo=timezone.utc)
_WINDOW_END = datetime(_YEAR, 5, 28, tzinfo=timezone.utc)


def _refuse_connect(_url: str) -> _FakeConnection:
    """Connect factory that must never run: the `conn=` escape hatch
    supplies the connection, so any call here means a helper wrongly
    opened its own socket instead of reusing the passed one.
    """
    raise AssertionError("connect= must not be called when conn= is supplied")


class ConnReusePathTest(unittest.TestCase):
    """The `conn=` kwarg on every public read helper lets a caller
    (typically the dashboard inside an `analytics_connection` scope)
    reuse a single connection across many reads instead of paying
    the per-call handshake. When `conn=` is provided the helper
    runs the query directly on that connection without ever calling
    the `connect=` factory or closing the connection.
    """

    def test_get_summary_reuses_passed_conn(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # A single combined-SQL row is enough so the reader does not
        # short-circuit. Values themselves are irrelevant here.
        conn.rows_for = {_WIN_CTE: [_ZERO_TOTALS_ROW]}
        opens: list[str] = []
        analytics_read.get_summary(
            connect=lambda url: opens.append(url) or _FakeConnection(),
            conn=conn,
        )
        self.assertEqual(opens, [])  # factory never called
        # Layer 3 collapses totals + by_event + by_stage into one
        # round-trip on the provided connection.
        self.assertEqual(len(conn.executed), 1)
        # The reuse path never closes the caller's connection.
        self.assertEqual(conn.close_called, 0)

    def test_filter_options_uses_passed_connection(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_filter_options(
            connect=lambda url: _FakeConnection(),  # must not be used
            conn=conn,
        )
        # Layer 3 unions the five distinct-column queries into one.
        self.assertEqual(len(conn.executed), 1)
        self.assertEqual(conn.close_called, 0)

    def test_get_kpi_prev_reuses_passed_conn(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            "AS total_cost_usd": [(1.23, 100, 200, 50, 25, 3)],
        }
        opens: list[str] = []
        prev_summary = analytics_read.get_kpi_prev(
            connect=lambda url: opens.append(url) or _FakeConnection(),
            conn=conn,
        )
        self.assertEqual(opens, [])
        self.assertEqual(len(conn.executed), 1)
        self.assertEqual(conn.close_called, 0)
        # Each KPI scalar round-trips from its row column on the reused
        # connection.
        self.assertEqual(prev_summary.total_cost_usd, 1.23)
        self.assertEqual(prev_summary.total_input_tokens, 100)
        self.assertEqual(prev_summary.total_output_tokens, 200)
        self.assertEqual(prev_summary.total_cache_read_tokens, 50)
        self.assertEqual(prev_summary.total_cache_write_tokens, 25)
        self.assertEqual(prev_summary.total_agent_runs, 3)

    def test_get_data_extent_reuses_passed_conn(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            "data_min_ts": [(_WINDOW_START, _WINDOW_END)],
        }
        extent = analytics_read.get_data_extent(
            connect=lambda url: _FakeConnection(), conn=conn,
        )
        self.assertIsNotNone(extent.min_ts)
        self.assertIsNotNone(extent.max_ts)
        self.assertEqual(conn.close_called, 0)

    def test_passed_conn_works_without_global_url(self) -> None:
        # The `conn=` path is a complete escape hatch: a caller that
        # already holds a connection (e.g. opened with an explicit
        # `analytics_connection(db_url=...)`) must be able to run
        # every helper without the global `ANALYTICS_DB_URL` being
        # set. Without this, `with analytics_connection(db_url=X) as c:
        # get_data_extent(conn=c)` would silently return
        # `DataExtent()` unless the caller also repeated `db_url=X`
        # on every helper. `_refuse_connect` proves the connect-factory
        # path is never exercised.
        analytics_read = _reload_read(db_url="")

        # `get_data_extent` -- single query, easy to assert.
        extent_conn = _FakeConnection()
        extent_conn.rows_for = {
            "data_min_ts": [(_WINDOW_START, _WINDOW_END)],
        }
        self.assertIsNotNone(
            analytics_read.get_data_extent(
                conn=extent_conn, connect=_refuse_connect,
            ).min_ts,
        )
        self.assertEqual(len(extent_conn.executed), 1)
        self.assertEqual(extent_conn.close_called, 0)

        # `get_filter_options` -- one unioned query on the same
        # connection. A fresh fake avoids needle collisions with
        # other helpers.
        opts_conn = _FakeConnection()
        opts_conn.rows_for = {
            "UNION SELECT 'event' AS dim": [("repo", "owner/a")],
        }
        self.assertEqual(
            analytics_read.get_filter_options(
                conn=opts_conn, connect=_refuse_connect,
            ).repos,
            ("owner/a",),
        )
        self.assertEqual(len(opts_conn.executed), 1)

        # `get_summary` -- totals + by_event + by_stage collapsed
        # into one round-trip on the provided connection.
        summary_conn = _FakeConnection()
        summary_conn.rows_for = {_WIN_CTE: [_ZERO_TOTALS_ROW]}
        analytics_read.get_summary(conn=summary_conn, connect=_refuse_connect)
        self.assertEqual(len(summary_conn.executed), 1)
        self.assertEqual(summary_conn.close_called, 0)

        # `get_time_series` -- single query, exercises a view-free
        # base-table helper to round out the coverage.
        ts_conn = _FakeConnection()
        analytics_read.get_time_series(conn=ts_conn, connect=_refuse_connect)
        self.assertEqual(len(ts_conn.executed), 1)
        self.assertEqual(ts_conn.close_called, 0)

    def test_none_uses_legacy_open_close_path(self) -> None:
        # Backwards-compat: callers that do not pass `conn=` still
        # see the existing one-connection-per-call shape so the
        # original tests (and any other consumers) keep working.
        # After Layer 3 `get_summary` is a single query so the
        # invariant tightens to one open / one close.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {_WIN_CTE: [_ZERO_TOTALS_ROW]}
        analytics_read.get_summary(connect=conn.as_connect)
        self.assertEqual(len(conn.executed), 1)
        self.assertEqual(conn.close_called, 1)


if __name__ == "__main__":
    unittest.main()
