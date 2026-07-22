# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics stage-breakdown read tests."""

import unittest


from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)


from tests.analytics_assertions import (
    assert_row_fields,
    assert_sql_fragments,
)


_TOKENS_RUNS_CACHE_CACHE_COST_USD = 0.3


_TOKENS_RUNS_CACHE_NO_CACHE_COST_USD = 0.2


_TOKENS_RUNS_CACHE_CACHE_COST_USD_SECONDARY = 0.04


_TOKENS_RUNS_CACHE_NO_CACHE_COST_SECONDARY = 0.06


_ROLLUP_SCAN = "FROM analytics_daily_rollup"


_AGENT_EXIT = "agent_exit"


_STAGE_ENTER = "stage_enter"


_STAGE_IMPLEMENTING = "implementing"


_STAGE_VALIDATING = "validating"


_IMPL_EVENTS = 20


_IMPL_AVG_DURATION_S = 12.5


_IMPL_INPUT_TOKENS = 2000


_IMPL_OUTPUT_TOKENS = 1500
_RUNS_FIELD = "runs"
_CACHE_COST_FIELD = "cache_cost_usd"
_NO_CACHE_COST_FIELD = "no_cache_cost_usd"


class StageEventBreakdownTest(unittest.TestCase):
    def test_stage_breakdown_empty_when_db_url_unset(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_stage_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_stage_breakdown_handles_null_avg(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # Rollup-backed: the SQL recovers `AVG(duration_s)` as
        # `SUM(duration_s_sum) / NULLIF(SUM(duration_s_count), 0)`.
        # The fake fixture pre-computes that ratio so the reader's
        # NULL handling still rides through.
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (_STAGE_IMPLEMENTING, _IMPL_EVENTS, _IMPL_AVG_DURATION_S),
                (_STAGE_VALIDATING, 10, None),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=conn.as_connect)
        # NULL avg duration survives as None, not a coerced zero.
        assert_row_fields(
            self,
            rows[0],
            {
                "stage": _STAGE_IMPLEMENTING,
                "count": _IMPL_EVENTS,
                "avg_duration_s": _IMPL_AVG_DURATION_S,
            },
        )
        self.assertIsNone(rows[1].avg_duration_s)
        sql, _ = conn.first_query
        # `IS NOT NULL` guard on stage is still present.
        self.assertIn("stage IS NOT NULL", sql)
        # Weighted-duration recovery from the rollup, not a
        # base-table `AVG(duration_s)`.
        self.assertIn("SUM(duration_s_sum)", sql)
        self.assertIn("NULLIF(SUM(duration_s_count), 0)", sql)

    def test_event_breakdown_returns_rows(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            "GROUP BY event": [(_AGENT_EXIT, 5), (_STAGE_ENTER, 3)],
        }
        rows = analytics_read.get_event_breakdown(connect=conn.as_connect)
        assert_row_fields(self, rows[0], {"event": _AGENT_EXIT, "count": 5})
        assert_row_fields(self, rows[1], {"event": _STAGE_ENTER, "count": 3})


class StageBreakdownExtensionTest(unittest.TestCase):
    """Extended `get_stage_breakdown` rolls up cost / token totals
    plus an agent-run subset count per stage so the redesigned "Cost
    by workflow stage" panel can label its sub-line as "runs"
    against the per-stage cost. The cost is further split into
    cache_cost_usd / no_cache_cost_usd so the panel can stack
    cache vs no-cache spend per stage."""

    def test_rolls_up_cost_tokens_runs_and_cache(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # 9-tuple shape: stage / events / avg_dur / cost / input /
        # output / agent-run subset / cache_cost / no_cache_cost.
        # The reader reads from the daily rollup so the SQL aggregates
        # the rollup's `total_*` columns instead of the raw events
        # table; the cache split is prorated per rollup row by
        # token share so cache + no-cache sums back to the stage's
        # total cost.
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (
                    _STAGE_IMPLEMENTING,
                    _IMPL_EVENTS,
                    _IMPL_AVG_DURATION_S,
                    0.5,
                    _IMPL_INPUT_TOKENS,
                    _IMPL_OUTPUT_TOKENS,
                    8,
                    0.3,
                    0.2,
                ),
                (_STAGE_VALIDATING, 10, None, 0.1, 100, 200, 3, 0.04, 0.06),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=conn.as_connect)
        assert_row_fields(
            self,
            rows[0],
            {
                "total_cost_usd": 0.5,
                "total_input_tokens": _IMPL_INPUT_TOKENS,
                "total_output_tokens": _IMPL_OUTPUT_TOKENS,
                _RUNS_FIELD: 8,
                _CACHE_COST_FIELD: _TOKENS_RUNS_CACHE_CACHE_COST_USD,
                _NO_CACHE_COST_FIELD: _TOKENS_RUNS_CACHE_NO_CACHE_COST_USD,
            },
        )
        assert_row_fields(
            self,
            rows[1],
            {
                "total_cost_usd": 0.1,
                _RUNS_FIELD: 3,
                _CACHE_COST_FIELD: _TOKENS_RUNS_CACHE_CACHE_COST_USD_SECONDARY,
                _NO_CACHE_COST_FIELD: _TOKENS_RUNS_CACHE_NO_CACHE_COST_SECONDARY,
            },
        )
        sql, _ = conn.first_query
        self.assertIn("SUM(total_cost_usd)", sql)
        # Agent-run subset uses `event = 'agent_exit'`, scoped by
        # the same WHERE clause as the totals so the per-stage sub-
        # line lines up with the per-stage cost.
        self.assertIn("event = 'agent_exit'", sql)
        # Cache / no-cache split is proportional: each rollup row's
        # cost is weighted by the cache-token share of its billable
        # token volume. `total_cached_tokens` is Codex's subset of
        # `total_input_tokens`, so it appears in the numerator only.
        assert_sql_fragments(
            self,
            sql,
            (
                "total_cached_tokens",
                "total_cache_read_tokens",
                "total_cache_write_tokens",
                "stage_cache_cost_usd",
                "stage_no_cache_cost_usd",
            ),
        )

    def test_legacy_seven_tuple_fixture_round_trips(self) -> None:
        # Older fixtures still emit 7-tuple `(stage, count, avg_dur,
        # cost, in, out, runs)` rows without the cache split; the
        # reader defaults `cache_cost_usd` / `no_cache_cost_usd` to
        # zero so unrelated tests round-trip.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (
                    _STAGE_IMPLEMENTING,
                    _IMPL_EVENTS,
                    _IMPL_AVG_DURATION_S,
                    0.5,
                    _IMPL_INPUT_TOKENS,
                    _IMPL_OUTPUT_TOKENS,
                    8,
                ),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=conn.as_connect)
        # `runs` round-trips; the missing cache split defaults to a
        # zero cost on both bands.
        assert_row_fields(
            self,
            rows[0],
            {_RUNS_FIELD: 8, _CACHE_COST_FIELD: float(), _NO_CACHE_COST_FIELD: float()},
        )

    def test_legacy_six_tuple_fixture_round_trips(self) -> None:
        # Older fixtures still emit 6-tuple `(stage, count, avg_dur,
        # cost, in, out)` rows without the agent-run subset; the
        # reader defaults `runs` to zero so unrelated tests round-
        # trip.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _ROLLUP_SCAN: [
                (_STAGE_IMPLEMENTING, _IMPL_EVENTS, _IMPL_AVG_DURATION_S, 0.5, _IMPL_INPUT_TOKENS, _IMPL_OUTPUT_TOKENS),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=conn.as_connect)
        # `runs` and both cache bands default to zero.
        assert_row_fields(
            self,
            rows[0],
            {_RUNS_FIELD: 0, _CACHE_COST_FIELD: float(), _NO_CACHE_COST_FIELD: float()},
        )
