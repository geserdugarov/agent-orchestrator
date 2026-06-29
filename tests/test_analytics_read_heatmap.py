# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.analytics_read_helpers import (
    _FakeConnection,
    _connector,
    _reload,
)


class HourlyHeatmapTest(unittest.TestCase):
    """`get_hourly_heatmap` returns (weekday, hour, count) cells
    aggregated from the base table; the chart layer fills in the
    rest of the 7x24 grid."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_hourly_heatmap(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_cells_round_trip(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 4-tuple: weekday / hour / event count / per-cell tokens.
        # The token column powers the redesigned "Token volume by
        # hour x weekday" heatmap; the event count is kept for
        # callers that want activity rather than token volume.
        conn.rows_for = {
            "EXTRACT(DOW FROM": [
                (1, 9, 5, 25_000),
                (1, 14, 7, 40_000),
                (3, 22, 2, 4_500),
            ],
        }
        cells = analytics_read.get_hourly_heatmap(connect=_connector(conn))
        self.assertEqual(len(cells), 3)
        self.assertEqual(
            (cells[0].weekday, cells[0].hour, cells[0].count,
             cells[0].total_tokens),
            (1, 9, 5, 25_000),
        )
        sql, _ = conn.executed[0]
        self.assertIn("EXTRACT(DOW FROM", sql)
        self.assertIn("EXTRACT(HOUR FROM", sql)
        self.assertIn("FROM analytics_events", sql)
        # SQL totals input + output + cache_read + cache_write so
        # the matrix renders token volume rather than event count.
        for col in (
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens",
        ):
            self.assertIn(col, sql)

    def test_legacy_three_tuple_rows_default_tokens_to_zero(self) -> None:
        # Older fixtures still emit 3-tuple `(weekday, hour, count)`
        # rows without the token column; the reader defaults the
        # token total to zero so unrelated tests round-trip.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "EXTRACT(DOW FROM": [(1, 9, 5)],
        }
        cells = analytics_read.get_hourly_heatmap(connect=_connector(conn))
        self.assertEqual(cells[0].total_tokens, 0)

    def test_event_filter_threaded(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_hourly_heatmap(
            events=["agent_exit"], connect=_connector(conn),
        )
        sql, params = conn.executed[0]
        self.assertIn("event IN (%s)", sql)
        self.assertIn("agent_exit", params)

    def test_tz_offset_zero_is_default(self) -> None:
        # Default omits any explicit offset; the SQL still applies the
        # offset arithmetic uniformly (offset = 0 leaves the bucketing
        # identical to plain UTC) so the read shape is the same.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_hourly_heatmap(connect=_connector(conn))
        sql, params = conn.executed[0]
        # `ts` is normalized to UTC before the offset is added so the
        # bucketing does not silently re-shift on a non-UTC session.
        self.assertIn("ts AT TIME ZONE 'UTC'", sql)
        self.assertIn("%s * INTERVAL '1 hour'", sql)
        # Two extractions (DOW + HOUR) each take the offset placeholder,
        # so the offset binds twice as the leading two params.
        self.assertEqual(params[0], 0)
        self.assertEqual(params[1], 0)

    def test_tz_offset_threaded_into_sql_params(self) -> None:
        # When the dashboard selects a non-zero UTC offset, the integer
        # binds as the first two SQL params (DOW + HOUR extractions).
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_hourly_heatmap(
            tz_offset_hours=7, connect=_connector(conn),
        )
        _, params = conn.executed[0]
        self.assertEqual(params[0], 7)
        self.assertEqual(params[1], 7)

    def test_tz_offset_negative(self) -> None:
        # Western timezones bind a negative integer; the SQL applies
        # `ts + -5 * INTERVAL '1 hour'` which Postgres reduces to a
        # five-hour shift backwards.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_hourly_heatmap(
            tz_offset_hours=-5, connect=_connector(conn),
        )
        _, params = conn.executed[0]
        self.assertEqual(params[0], -5)
        self.assertEqual(params[1], -5)
