# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

# The weekday extraction is the reader's most distinctive SQL
# fragment, so tests register the fake's cells against it and assert
# it lands in the emitted query.
_DOW_EXTRACT = "EXTRACT(DOW FROM"


class HourlyHeatmapTest(unittest.TestCase):
    """`get_hourly_heatmap` returns (weekday, hour, count) cells
    aggregated from the base table; the chart layer fills in the
    rest of the 7x24 grid."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_hourly_heatmap(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_cells_round_trip(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # 4-tuple: weekday / hour / event count / per-cell tokens.
        # The token column powers the redesigned "Token volume by
        # hour x weekday" heatmap; the event count is kept for
        # callers that want activity rather than token volume.
        conn.rows_for = {
            _DOW_EXTRACT: [
                (1, 9, 5, 25_000),
                (1, 14, 7, 40_000),
                (3, 22, 2, 4_500),
            ],
        }
        cells = analytics_read.get_hourly_heatmap(connect=conn.as_connect)
        self.assertEqual(len(cells), 3)
        self.assertEqual(
            (cells[0].weekday, cells[0].hour, cells[0].count,
             cells[0].total_tokens),
            (1, 9, 5, 25_000),
        )
        sql, _ = conn.first_query
        self.assertIn(_DOW_EXTRACT, sql)
        self.assertIn("EXTRACT(HOUR FROM", sql)
        self.assertIn("FROM analytics_events", sql)
        # SQL totals input + output + cache_read + cache_write so
        # the matrix renders token volume rather than event count.
        for token_column in (
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens",
        ):
            self.assertIn(token_column, sql)

    def test_legacy_three_tuple_tokens_default_zero(self) -> None:
        # Older fixtures still emit 3-tuple `(weekday, hour, count)`
        # rows without the token column; the reader defaults the
        # token total to zero so unrelated tests round-trip.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _DOW_EXTRACT: [(1, 9, 5)],
        }
        cells = analytics_read.get_hourly_heatmap(connect=conn.as_connect)
        self.assertEqual(cells[0].total_tokens, 0)

    def test_event_filter_threaded(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_hourly_heatmap(
            events=["agent_exit"], connect=conn.as_connect,
        )
        sql, query_params = conn.first_query
        self.assertIn("event IN (%s)", sql)
        self.assertIn("agent_exit", query_params)

    def test_tz_offset_zero_is_default(self) -> None:
        # Default omits any explicit offset; the SQL still applies the
        # offset arithmetic uniformly (offset = 0 leaves the bucketing
        # identical to plain UTC) so the read shape is the same.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_hourly_heatmap(connect=conn.as_connect)
        sql, query_params = conn.first_query
        # `ts` is normalized to UTC before the offset is added so the
        # bucketing does not silently re-shift on a non-UTC session.
        self.assertIn("ts AT TIME ZONE 'UTC'", sql)
        self.assertIn("%s * INTERVAL '1 hour'", sql)
        # Two extractions (DOW + HOUR) each take the offset placeholder,
        # so the offset binds twice as the leading two params.
        self.assertEqual(query_params[0], 0)
        self.assertEqual(query_params[1], 0)

    def test_tz_offset_threaded_into_sql_params(self) -> None:
        # A selected UTC offset binds as the first two SQL params
        # (the DOW + HOUR extractions). Western (negative) offsets
        # bind a negative integer, which Postgres reduces to a
        # backwards shift.
        analytics_read = _reload_read()
        for tz_offset in (7, -5):
            with self.subTest(tz_offset=tz_offset):
                conn = _FakeConnection()
                analytics_read.get_hourly_heatmap(
                    tz_offset_hours=tz_offset, connect=conn.as_connect,
                )
                _, query_params = conn.first_query
                self.assertEqual(query_params[0], tz_offset)
                self.assertEqual(query_params[1], tz_offset)
