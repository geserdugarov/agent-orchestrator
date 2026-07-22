# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics backend token and efficiency read tests."""

import unittest


from datetime import date


from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)
from tests.analytics_assertions import (
    assert_row_fields,
    assert_sql_fragments,
)

_AGGREGATES_ROUND_TRIP_RUNS = 20
_AGGREGATES_ROUND_TRIP_AVG_DURATION_S = 35
_AGGREGATES_ROUND_TRIP_TOTAL_COST_USD = 1.2
_AGGREGATES_ROUND_TRIP_TOTAL_INPUT_TOKENS = 5000
_AGGREGATES_ROUND_TRIP_TOTAL_OUTPUT_TOKENS = 4000
_AGGREGATES_ROUND_TRIP_TOTAL_CACHE_READ_TOK = 1500
_AGGREGATES_ROUND_TRIP_TOTAL_CACHE_WRITE_TO = 800


_STAGE_ENTER = "stage_enter"


_AGENT_RUNS_VIEW = "analytics_agent_runs"


_ROLLUP_SCAN = "FROM analytics_daily_rollup"


_EVENT_AGENT_EXIT = "event = 'agent_exit'"


_CLAUDE = "claude"


_CODEX = "codex"


_UNKNOWN = "unknown"


_YEAR = 2026


_DAY_ONE = date(_YEAR, 5, 1)


_DAY_TWO = date(_YEAR, 5, 2)


class BackendDailyTokensTest(unittest.TestCase):
    """`get_backend_daily_tokens` powers the redesigned dashboard's
    "By backend" hero toggle. It must read from the view, honor the
    agent-run event-filter short-circuit, and aggregate tokens across
    every agent run in the window (not a `LIMIT`-capped subset).
    """

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_backend_daily_tokens(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_backend_daily_tokens(
            events=[_STAGE_ENTER],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_backend_daily_tokens(
            events=[],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_reads_daily_backend_totals_from_view(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _AGENT_RUNS_VIEW: [
                (_DAY_ONE, _CLAUDE, 12_000),
                (_DAY_ONE, _CODEX, 4_500),
                (_DAY_TWO, _CLAUDE, 8_000),
            ],
        }
        rows = analytics_read.get_backend_daily_tokens(connect=conn.as_connect)
        self.assertEqual(
            [(row.day, row.backend, row.total_tokens) for row in rows],
            [
                (_DAY_ONE, _CLAUDE, 12_000),
                (_DAY_ONE, _CODEX, 4_500),
                (_DAY_TWO, _CLAUDE, 8_000),
            ],
        )
        sql, _ = conn.first_query
        # Reads from the view -- so the agent-run filter contract
        # (no `event IN` clause) holds -- and groups by both day and
        # backend so the dashboard can build a per-day stack without
        # post-processing. Token total includes the cache band so the
        # backend stack matches the standalone mock's
        # `input + output + cache_read + cache_write` accounting.
        self.assertIn("FROM analytics_agent_runs", sql)
        self.assertNotIn("event IN", sql)
        self.assertIn("GROUP BY day, backend_label", sql)
        for token_column in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            self.assertIn(token_column, sql)

    def test_null_backend_buckets_under_unknown(self) -> None:
        # `COALESCE(backend, 'unknown')` matches how
        # `get_backend_efficiency` surfaces NULL-backend rows.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _AGENT_RUNS_VIEW: [
                (_DAY_ONE, _UNKNOWN, 1_000),
            ],
        }
        rows = analytics_read.get_backend_daily_tokens(connect=conn.as_connect)
        self.assertEqual([row.backend for row in rows], [_UNKNOWN])


class BackendEfficiencyTest(unittest.TestCase):
    """`get_backend_efficiency` aggregates the agent_runs view by
    backend and exposes failure / cost / token rollups."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_backend_efficiency(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_backend_efficiency(
            events=[_STAGE_ENTER],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_aggregates_round_trip(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # 9-tuple: backend / runs / failed / avg_dur / cost /
        # input_tokens / output_tokens / cache_read / cache_write. The
        # reader reads the daily rollup (with `event = 'agent_exit'`
        # pinned to match the agent-runs view filter); the fixture
        # pre-computes the weighted average so the reader's NULL
        # handling still rides through. Cache columns feed the
        # per-backend "cost / 1M tok" tile alongside input + output.
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (_CLAUDE, 20, 1, 35, 1.2, 5000, 4000, 1500, 800),
                (_CODEX, 10, 3, None, 0.4, 1000, 2000, 0, 0),
                (_UNKNOWN, 1, 0, None, 0, 0, 0, 0, 0),
            ],
        }
        rows = analytics_read.get_backend_efficiency(connect=conn.as_connect)
        self.assertEqual(
            [row.backend for row in rows],
            [_CLAUDE, _CODEX, _UNKNOWN],
        )
        assert_row_fields(
            self,
            rows[0],
            {
                "runs": _AGGREGATES_ROUND_TRIP_RUNS,
                "failed": 1,
                "avg_duration_s": _AGGREGATES_ROUND_TRIP_AVG_DURATION_S,
                "total_cost_usd": _AGGREGATES_ROUND_TRIP_TOTAL_COST_USD,
                "total_input_tokens": _AGGREGATES_ROUND_TRIP_TOTAL_INPUT_TOKENS,
                "total_output_tokens": _AGGREGATES_ROUND_TRIP_TOTAL_OUTPUT_TOKENS,
                "total_cache_read_tokens": _AGGREGATES_ROUND_TRIP_TOTAL_CACHE_READ_TOK,
                "total_cache_write_tokens": _AGGREGATES_ROUND_TRIP_TOTAL_CACHE_WRITE_TO,
            },
        )
        # NULL avg duration preserved so the dashboard can hide the
        # column rather than show a misleading zero.
        self.assertIsNone(rows[1].avg_duration_s)
        sql, _ = conn.first_query
        # The rollup carries an `event` column, so the cutover query
        # pins `event = 'agent_exit'` directly rather than the view's
        # implicit filter. Weighted-duration recovery comes from the
        # rollup's duration sums, not `AVG(duration_s)` over the raw
        # events table.
        assert_sql_fragments(
            self,
            sql,
            (
                _ROLLUP_SCAN,
                _EVENT_AGENT_EXIT,
                "COALESCE(backend, 'unknown')",
                "SUM(total_cache_read_tokens)",
                "SUM(total_cache_write_tokens)",
                "SUM(duration_s_sum)",
                "NULLIF(SUM(duration_s_count), 0)",
            ),
        )

    def test_seven_tuple_defaults_cache_to_zero(self) -> None:
        # Older 7-tuple `(backend, runs, failed, avg_dur, cost, in,
        # out)` rows still round-trip with zero cache tokens so
        # unrelated tests keep working.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (_CLAUDE, 5, 0, 10, 0.2, 1000, 500),
            ],
        }
        rows = analytics_read.get_backend_efficiency(connect=conn.as_connect)
        self.assertEqual(rows[0].total_cache_read_tokens, 0)
        self.assertEqual(rows[0].total_cache_write_tokens, 0)
