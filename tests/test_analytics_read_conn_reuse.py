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


class ConnReusePathTest(unittest.TestCase):
    """The `conn=` kwarg on every public read helper lets a caller
    (typically the dashboard inside an `analytics_connection` scope)
    reuse a single connection across many reads instead of paying
    the per-call handshake. When `conn=` is provided the helper
    runs the query directly on that connection without ever calling
    the `connect=` factory or closing the connection.
    """

    def test_get_summary_reuses_passed_conn_without_calling_factory(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # A single combined-SQL row is enough so the reader does not
        # short-circuit. Values themselves are irrelevant here.
        conn.rows_for = {
            "WITH win AS": [("t", None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)],
        }
        opens: list[str] = []

        def _factory(url: str) -> _FakeConnection:
            opens.append(url)
            return _FakeConnection()

        analytics_read.get_summary(connect=_factory, conn=conn)
        self.assertEqual(opens, [])  # factory never called
        # Layer 3 collapses totals + by_event + by_stage into one
        # round-trip on the provided connection.
        self.assertEqual(len(conn.executed), 1)
        # The reuse path never closes the caller's connection.
        self.assertEqual(conn.close_called, 0)

    def test_get_filter_options_runs_one_query_on_passed_conn(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_filter_options(
            connect=lambda url: _FakeConnection(),  # must not be used
            conn=conn,
        )
        # Layer 3 unions the five distinct-column queries into one.
        self.assertEqual(len(conn.executed), 1)
        self.assertEqual(conn.close_called, 0)

    def test_get_kpi_prev_reuses_passed_conn(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "AS total_cost_usd": [(1.23, 100, 200, 50, 25, 3)],
        }
        opens: list[str] = []

        def _factory(url: str) -> _FakeConnection:
            opens.append(url)
            return _FakeConnection()

        result = analytics_read.get_kpi_prev(connect=_factory, conn=conn)
        self.assertEqual(opens, [])
        self.assertEqual(len(conn.executed), 1)
        self.assertEqual(conn.close_called, 0)
        self.assertEqual(result.total_cost_usd, 1.23)
        self.assertEqual(result.total_input_tokens, 100)
        self.assertEqual(result.total_output_tokens, 200)
        self.assertEqual(result.total_cache_read_tokens, 50)
        self.assertEqual(result.total_cache_write_tokens, 25)
        self.assertEqual(result.total_agent_runs, 3)

    def test_get_data_extent_reuses_passed_conn(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "data_min_ts": [
                (
                    datetime(2026, 5, 1, tzinfo=timezone.utc),
                    datetime(2026, 5, 28, tzinfo=timezone.utc),
                )
            ]
        }
        extent = analytics_read.get_data_extent(
            connect=lambda url: _FakeConnection(), conn=conn,
        )
        self.assertIsNotNone(extent.min_ts)
        self.assertIsNotNone(extent.max_ts)
        self.assertEqual(conn.close_called, 0)

    def test_conn_runs_query_even_when_global_db_url_unset(self) -> None:
        # The `conn=` path is a complete escape hatch: a caller that
        # already holds a connection (e.g. opened with an explicit
        # `analytics_connection(db_url=...)`) must be able to run
        # every helper without the global `ANALYTICS_DB_URL` being
        # set. Without this, `with analytics_connection(db_url=X) as c:
        # get_data_extent(conn=c)` would silently return
        # `DataExtent()` unless the caller also repeated `db_url=X`
        # on every helper.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        # `_never_called` proves the connect-factory path is not
        # exercised: the only way it would land is if a helper
        # short-circuited on the unset URL and then tried to open
        # its own socket, which is the bug we are guarding against.
        def _never_called(_url: str) -> _FakeConnection:
            raise AssertionError(
                "connect= must not be called when conn= is supplied"
            )

        # `get_data_extent` -- single query, easy to assert.
        extent_conn = _FakeConnection()
        extent_conn.rows_for = {
            "data_min_ts": [
                (
                    datetime(2026, 5, 1, tzinfo=timezone.utc),
                    datetime(2026, 5, 28, tzinfo=timezone.utc),
                )
            ],
        }
        extent = analytics_read.get_data_extent(
            conn=extent_conn, connect=_never_called,
        )
        self.assertEqual(len(extent_conn.executed), 1)
        self.assertEqual(extent_conn.close_called, 0)
        self.assertIsNotNone(extent.min_ts)

        # `get_filter_options` -- one unioned query on the same
        # connection. A fresh fake avoids needle collisions with
        # other helpers.
        opts_conn = _FakeConnection()
        opts_conn.rows_for = {
            "UNION SELECT 'event' AS dim": [("repo", "owner/a")],
        }
        opts = analytics_read.get_filter_options(
            conn=opts_conn, connect=_never_called,
        )
        self.assertEqual(len(opts_conn.executed), 1)
        self.assertEqual(opts.repos, ("owner/a",))

        # `get_summary` -- totals + by_event + by_stage collapsed
        # into one round-trip on the provided connection.
        summary_conn = _FakeConnection()
        summary_conn.rows_for = {
            "WITH win AS": [
                ("t", None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            ],
        }
        analytics_read.get_summary(conn=summary_conn, connect=_never_called)
        self.assertEqual(len(summary_conn.executed), 1)
        self.assertEqual(summary_conn.close_called, 0)

        # `get_time_series` -- single query, exercises a view-free
        # base-table helper to round out the coverage.
        ts_conn = _FakeConnection()
        analytics_read.get_time_series(
            conn=ts_conn, connect=_never_called,
        )
        self.assertEqual(len(ts_conn.executed), 1)
        self.assertEqual(ts_conn.close_called, 0)

    def test_conn_none_preserves_legacy_open_close_path(self) -> None:
        # Backwards-compat: callers that do not pass `conn=` still
        # see the existing one-connection-per-call shape so the
        # original tests (and any other consumers) keep working.
        # After Layer 3 `get_summary` is a single query so the
        # invariant tightens to one open / one close.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            ],
        }
        analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(len(conn.executed), 1)
        self.assertEqual(conn.close_called, 1)


if __name__ == "__main__":
    unittest.main()
