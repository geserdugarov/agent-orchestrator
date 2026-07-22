# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics repository, cost-coverage, and review-round read tests."""

import unittest


from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)
from tests.analytics_assertions import (
    assert_column_values,
    assert_row_fields,
    assert_sql_fragments,
)

_PER_REPO_ROWS_EVENTS = 30
_PRICE_PRESERVED_VERBATIM_TOTAL_TOKENS = 800_000
_PRICE_PRESERVED_VERBATIM_TOTAL_T_SECONDARY = 60_000


_STAGE_ENTER = "stage_enter"


_AGENT_RUNS_VIEW = "analytics_agent_runs"


_ROLLUP_SCAN = "FROM analytics_daily_rollup"


_UNKNOWN = "unknown"


_UNKNOWN_PRICE = "unknown-price"


class RepoBreakdownTest(unittest.TestCase):
    """`get_repo_breakdown` reads the base table so the standard
    event/stage/date/repo/issue filter shape applies (no agent_runs
    short-circuit)."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_repo_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_per_repo_rows(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            "GROUP BY repo": [
                ("owner/a", 5, 30, 4, 0.5),
                ("owner/b", 2, 10, 1, 0.1),
            ],
        }
        rows = analytics_read.get_repo_breakdown(connect=conn.as_connect)
        assert_row_fields(
            self,
            rows[0],
            {
                "repo": "owner/a",
                "issues": 5,
                "events": _PER_REPO_ROWS_EVENTS,
                "agent_exits": 4,
                "total_cost_usd": 0.5,
            },
        )
        sql, _ = conn.first_query
        # GROUP BY repo with distinct issue count per row -- safe
        # because rollup rows are already scoped to one repo per bucket
        # and the rollup key carries `issue`.
        self.assertIn("COUNT(DISTINCT issue)", sql)
        self.assertIn(_ROLLUP_SCAN, sql)

    def test_event_filter_threaded(self) -> None:
        # `get_repo_breakdown` honors the standard event filter because
        # it reads the base table (which carries an `event` column).
        # Cleared multiselect -> FALSE predicate.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_repo_breakdown(events=[], connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn("FALSE", sql)


class CostCoverageTest(unittest.TestCase):
    """`get_cost_coverage` MUST keep `unknown-price` visible -- it is
    the maintenance signal for the pricing table in
    `orchestrator.usage`. Distinct from rows whose `cost_source` is
    NULL, which bucket under the generic `"unknown"`."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_cost_coverage(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_cost_coverage(
            events=[_STAGE_ENTER],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_unknown_price_preserved_verbatim(self) -> None:
        conn = _FakeConnection()
        # The third tuple column is the per-`cost_source` token rollup
        # that feeds the redesigned token-share coverage bar.
        conn.rows_for = {
            _AGENT_RUNS_VIEW: [
                ("reported", 20, 800_000),
                ("estimated", 5, 100_000),
                (_UNKNOWN_PRICE, 3, 60_000),
                ("no-usage", 2, 20_000),
                (_UNKNOWN, 1, 5_000),
            ],
        }
        rows = _reload_read().get_cost_coverage(connect=conn.as_connect)
        by_source = {row.cost_source: row for row in rows}
        # The `unknown-price` slice surfaces with that exact label --
        # NEVER folded into "unknown" -- so the operator can see which
        # runs the parser could not price.
        self.assertIn(_UNKNOWN_PRICE, by_source)
        self.assertEqual(
            sum(1 for row in rows if row.cost_source == _UNKNOWN_PRICE),
            1,
        )
        self.assertEqual(
            sum(1 for row in rows if row.cost_source == _UNKNOWN),
            1,
        )
        # Per-source token volume rolls up alongside the run count.
        self.assertEqual(by_source["reported"].total_tokens, _PRICE_PRESERVED_VERBATIM_TOTAL_TOKENS)
        self.assertEqual(by_source[_UNKNOWN_PRICE].total_tokens, _PRICE_PRESERVED_VERBATIM_TOTAL_T_SECONDARY)
        sql, _ = conn.first_query
        self.assertIn("FROM analytics_agent_runs", sql)
        # NULL cost_source rows bucket under "unknown" via COALESCE, but
        # the verbatim "unknown-price" string is untouched. SQL totals
        # input + output + cache_read + cache_write so the token share
        # matches the standalone mock's accounting.
        self.assertIn("COALESCE(cost_source, 'unknown')", sql)
        for token_column in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            self.assertIn(token_column, sql)

    def test_legacy_two_tuple_defaults_tokens_to_zero(self) -> None:
        # Older 2-tuple `(cost_source, runs)` rows still round-trip; the
        # reader defaults `total_tokens` to zero so unrelated tests
        # round-trip.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _AGENT_RUNS_VIEW: [("reported", 3)],
        }
        rows = analytics_read.get_cost_coverage(connect=conn.as_connect)
        self.assertEqual([row.total_tokens for row in rows], [0])


class ReviewRoundBreakdownTest(unittest.TestCase):
    """`get_review_round_breakdown` reads from `analytics_agent_runs`
    so the agent-run filter contract (no `event` column in the view)
    is encoded as a Python-side short-circuit on `_agent_event_excluded`."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_review_round_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_review_round_breakdown(
            events=[_STAGE_ENTER],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_review_round_breakdown(
            events=[],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_view_query_buckets_rounds(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # 12-tuple rows carry the role + cache split the chart consumes:
        # (bucket, runs, failed, cost, dev_runs, rev_runs, dev_cost,
        # rev_cost, dev_cache, dev_no_cache, rev_cache, rev_no_cache).
        conn.rows_for = {
            _AGENT_RUNS_VIEW: [
                ("0", 12, 1, 40, 7, 5, 28, 12, 20, 8, 9, 3),
                ("1", 8, 2, 25, 4, 4, 10, 15, 7, 3, 11, 4),
                ("3-5", 4, 4, 18, 1, 3, 5, 13, 5, 0, 13, 0),
                (_UNKNOWN, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0),
            ],
        }
        rows = analytics_read.get_review_round_breakdown(connect=conn.as_connect)
        # Each column is checked independently against its own expected
        # values. `total_cost_usd` powers the "Cost by review round"
        # chart and "Rework share" tile; the per-role cache vs no-cache
        # split stacks so cache_cost + no_cache_cost equals the total.
        assert_column_values(
            self,
            rows,
            {
                "bucket": ["0", "1", "3-5", _UNKNOWN],
                "runs": [12, 8, 4, 1],
                "failed": [1, 2, 4, 0],
                "total_cost_usd": [40, 25, 18, 0],
                "developer_runs": [7, 4, 1, 1],
                "reviewer_runs": [5, 4, 3, 0],
                "developer_cost_usd": [28, 10, 5, 0],
                "reviewer_cost_usd": [12, 15, 13, 0],
                "developer_cache_cost_usd": [20, 7, 5, 0],
                "developer_no_cache_cost_usd": [8, 3, 0, 0],
                "reviewer_cache_cost_usd": [9, 11, 13, 0],
                "reviewer_no_cache_cost_usd": [3, 4, 0, 0],
            },
        )
        sql, _ = conn.first_query
        # Reads from the view (no `event` column, so no `event IN`
        # clause). The cache / no-cache split is proportional: each
        # run's cost is weighted by the cache-token share of its
        # billable token volume. Codex `cached_tokens` is already a
        # subset of `input_tokens`, so it appears in the numerator only.
        assert_sql_fragments(
            self,
            sql,
            (
                "FROM analytics_agent_runs",
                "SUM(cost_usd)",
                "agent_role IN ('developer', 'reviewer')",
                "agent_role = 'developer'",
                "agent_role = 'reviewer'",
                "stage = 'implementing' THEN '0'",
                "cached_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "developer_cache_cost_usd",
                "developer_no_cache_cost_usd",
                "reviewer_cache_cost_usd",
                "reviewer_no_cache_cost_usd",
            ),
        )
        self.assertNotIn("event IN", sql)

    def test_legacy_three_tuple_defaults_cost_to_zero(self) -> None:
        # Older 3-tuple `(bucket, runs, failed)` rows without the cost /
        # role / cache rollups still round-trip with those values
        # defaulted to zero.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {_AGENT_RUNS_VIEW: [("0", 3, 0)]}
        rows = analytics_read.get_review_round_breakdown(connect=conn.as_connect)
        self.assertEqual(len(rows), 1)
        assert_row_fields(
            self,
            rows[0],
            {
                "total_cost_usd": float(),
                "developer_cost_usd": float(),
                "reviewer_cost_usd": float(),
                "developer_cache_cost_usd": float(),
                "developer_no_cache_cost_usd": float(),
                "reviewer_cache_cost_usd": float(),
                "reviewer_no_cache_cost_usd": float(),
            },
        )

    def test_explicit_agent_exit_runs_query(self) -> None:
        # An events list that includes agent_exit must NOT short-circuit
        # -- the operator still wants to see the agent runs view.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {_AGENT_RUNS_VIEW: [("1", 3, 0, 5)]}
        rows = analytics_read.get_review_round_breakdown(
            events=["agent_exit", _STAGE_ENTER],
            connect=conn.as_connect,
        )
        self.assertEqual(len(rows), 1)
