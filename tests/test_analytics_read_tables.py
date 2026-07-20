# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

# SQL fragments and event / stage / repo literals the table readers
# thread through their queries; each recurs across the module.
_ROLLUP_SCAN = "FROM analytics_daily_rollup"
_GROUP_BY_PAIR = "GROUP BY repo, issue"
_AGENT_EXIT = "agent_exit"
_STAGE_ENTER = "stage_enter"
_STAGE_IMPLEMENTING = "implementing"
_STAGE_VALIDATING = "validating"
_REPO_SHORT = "owner/r"
_BACKEND_CLAUDE = "claude"
_AGENT_ROLE_DEV = "dev"

# The "implementing" stage-breakdown baseline metrics the legacy-tuple
# round-trip tests (3-, 6-, 7-, and 9-column fixtures) all build on.
_IMPL_EVENTS = 20
_IMPL_AVG_DURATION_S = 12.5
_IMPL_INPUT_TOKENS = 2000
_IMPL_OUTPUT_TOKENS = 1500

# Shared window bounds and reused event timestamps. `get_issues` /
# `get_recent_agent_exits` / `get_issue_events` scan the raw events
# table, so the `ts` predicates bind these datetimes directly.
_YEAR = 2026
_WINDOW_START = datetime(_YEAR, 5, 1, tzinfo=timezone.utc)
_WINDOW_END = datetime(_YEAR, 5, 28, tzinfo=timezone.utc)
_EVENT_TS = datetime(_YEAR, 5, 25, 10, 0, tzinfo=timezone.utc)
_NOON_TS = datetime(_YEAR, 5, 25, 12, 0, tzinfo=timezone.utc)
# `owner/b`'s issue row in `test_groups_by_repo_issue_pair` needs a
# distinct first_seen < last_seen pair; kept module-level so the
# fixture and its assertion bind the identical instants.
_LATER_SEEN = datetime(_YEAR, 5, 26, 9, 0, tzinfo=timezone.utc)
_LATEST_SEEN = datetime(_YEAR, 5, 26, 9, 30, tzinfo=timezone.utc)


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
        self.assertEqual(rows[0].stage, _STAGE_IMPLEMENTING)
        self.assertEqual(rows[0].count, _IMPL_EVENTS)
        self.assertEqual(rows[0].avg_duration_s, _IMPL_AVG_DURATION_S)
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
        self.assertEqual(rows[0].event, _AGENT_EXIT)
        self.assertEqual(rows[0].count, 5)
        self.assertEqual(rows[1].event, _STAGE_ENTER)
        self.assertEqual(rows[1].count, 3)


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
                    _STAGE_IMPLEMENTING, _IMPL_EVENTS, _IMPL_AVG_DURATION_S,
                    0.5, _IMPL_INPUT_TOKENS, _IMPL_OUTPUT_TOKENS, 8, 0.3, 0.2,
                ),
                (_STAGE_VALIDATING, 10, None, 0.1, 100, 200, 3, 0.04, 0.06),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=conn.as_connect)
        self.assertEqual(rows[0].total_cost_usd, 0.5)
        self.assertEqual(rows[0].total_input_tokens, _IMPL_INPUT_TOKENS)
        self.assertEqual(rows[0].total_output_tokens, _IMPL_OUTPUT_TOKENS)
        self.assertEqual(rows[0].runs, 8)
        self.assertEqual(rows[0].cache_cost_usd, 0.3)
        self.assertEqual(rows[0].no_cache_cost_usd, 0.2)
        self.assertEqual(rows[1].total_cost_usd, 0.1)
        self.assertEqual(rows[1].runs, 3)
        self.assertEqual(rows[1].cache_cost_usd, 0.04)
        self.assertEqual(rows[1].no_cache_cost_usd, 0.06)
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
        self.assertIn("total_cached_tokens", sql)
        self.assertIn("total_cache_read_tokens", sql)
        self.assertIn("total_cache_write_tokens", sql)
        self.assertIn("stage_cache_cost_usd", sql)
        self.assertIn("stage_no_cache_cost_usd", sql)

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
                    _STAGE_IMPLEMENTING, _IMPL_EVENTS, _IMPL_AVG_DURATION_S,
                    0.5, _IMPL_INPUT_TOKENS, _IMPL_OUTPUT_TOKENS, 8,
                ),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=conn.as_connect)
        # `runs` round-trips; the missing cache split defaults to a
        # zero cost on both bands.
        self.assertEqual(rows[0].runs, 8)
        self.assertEqual(rows[0].cache_cost_usd, 0.0)
        self.assertEqual(rows[0].no_cache_cost_usd, 0.0)

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
        self.assertEqual(rows[0].runs, 0)
        self.assertEqual(rows[0].cache_cost_usd, 0.0)
        self.assertEqual(rows[0].no_cache_cost_usd, 0.0)


class RecentAgentExitsTest(unittest.TestCase):

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_recent_agent_exits(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_non_positive_limit_short_circuits(self) -> None:
        analytics_read = _reload_read()
        connected = []
        exits = analytics_read.get_recent_agent_exits(
            limit=0,
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(exits, [])

    def test_returns_rows_filtered_to_agent_exit(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            "ORDER BY ts DESC LIMIT %s": [
                (
                    _NOON_TS, _REPO_SHORT, 7, _STAGE_IMPLEMENTING,
                    _AGENT_ROLE_DEV, _BACKEND_CLAUDE, 33.0, 0, False, 1, 0,
                    100, 200, 0.12, "cli",
                ),
            ],
        }
        exits = analytics_read.get_recent_agent_exits(
            limit=10, repo=_REPO_SHORT, connect=conn.as_connect,
        )
        self.assertEqual(len(exits), 1)
        exit_row = exits[0]
        self.assertEqual(exit_row.ts, _NOON_TS)
        self.assertEqual(exit_row.repo, _REPO_SHORT)
        self.assertEqual(exit_row.issue, 7)
        self.assertEqual(exit_row.stage, _STAGE_IMPLEMENTING)
        self.assertEqual(exit_row.agent_role, _AGENT_ROLE_DEV)
        self.assertEqual(exit_row.backend, _BACKEND_CLAUDE)
        self.assertEqual(exit_row.duration_s, 33.0)
        self.assertEqual(exit_row.exit_code, 0)
        self.assertFalse(exit_row.timed_out)
        self.assertEqual(exit_row.cost_usd, 0.12)
        self.assertEqual(exit_row.cost_source, "cli")
        # Query carries event='agent_exit' + repo filter + limit.
        sql, query_params = conn.first_query
        self.assertIn("event = %s", sql)
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(query_params, (_AGENT_EXIT, _REPO_SHORT, 10))


class IssuesOverviewTest(unittest.TestCase):
    """The dashboard's "issues" table: one row per `(repo, issue)`
    pair inside the window. Distinct from `get_issue_events` which
    drills into a single known issue."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        connected = []
        issues = analytics_read.get_issues(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(issues, [])

    def test_non_positive_limit_short_circuits(self) -> None:
        analytics_read = _reload_read()
        connected = []
        issues = analytics_read.get_issues(
            limit=0,
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(issues, [])

    def test_groups_by_repo_issue_pair(self) -> None:
        # Two issues sharing the bare issue number 1 across two repos
        # must surface as two distinct rows. This is the dashboard
        # complement to `test_distinct_issues_counts_repo_issue_pairs`
        # in SummaryTest.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _GROUP_BY_PAIR: [
                (
                    "owner/b", 1, 3, _LATER_SEEN, _LATEST_SEEN,
                    _STAGE_VALIDATING, 1, 0.42, 500, 300,
                ),
                (
                    "owner/a", 1, 5, _EVENT_TS, _NOON_TS,
                    _STAGE_IMPLEMENTING, 2, None, 0, 0,
                ),
            ],
        }
        issues = analytics_read.get_issues(connect=conn.as_connect)
        # Order preserved from the SQL; the second row's `None` cost
        # survives as `None` rather than coercing to 0.0.
        self.assertEqual(len(issues), 2)
        self.assertEqual((issues[0].repo, issues[0].issue), ("owner/b", 1))
        self.assertEqual((issues[1].repo, issues[1].issue), ("owner/a", 1))
        self.assertEqual(issues[0].event_count, 3)
        self.assertEqual(issues[0].first_seen, _LATER_SEEN)
        self.assertEqual(issues[0].last_seen, _LATEST_SEEN)
        self.assertEqual(issues[0].latest_stage, _STAGE_VALIDATING)
        self.assertEqual(issues[0].agent_exits, 1)
        self.assertEqual(issues[0].total_cost_usd, 0.42)
        self.assertEqual(issues[0].total_input_tokens, 500)
        self.assertEqual(issues[0].total_output_tokens, 300)
        self.assertIsNone(issues[1].total_cost_usd)
        # SQL shape: GROUP BY pair, ORDER BY last_seen DESC, LIMIT.
        sql, query_params = conn.first_query
        self.assertIn(_GROUP_BY_PAIR, sql)
        self.assertIn("ORDER BY last_seen DESC", sql)
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(query_params[-1], 100)

    def test_window_and_repo_params_bound(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_issues(
            start=_WINDOW_START, end=_WINDOW_END, repo=_REPO_SHORT, limit=25,
            connect=conn.as_connect,
        )
        sql, query_params = conn.first_query
        self.assertIn("ts >= %s", sql)
        self.assertIn("ts < %s", sql)
        self.assertIn("repo = %s", sql)
        self.assertEqual(
            query_params,
            (_WINDOW_START, _WINDOW_END, _REPO_SHORT, 25),
        )

    def test_null_latest_stage_survives(self) -> None:
        # `latest_stage` is None when no event for the issue in the
        # window carried a stage (e.g. only `agent_exit` rows whose
        # stage column happened to be null).
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _GROUP_BY_PAIR: [
                (_REPO_SHORT, 7, 1, _EVENT_TS, _EVENT_TS, None, 0, None, 0, 0),
            ],
        }
        rows = analytics_read.get_issues(connect=conn.as_connect)
        self.assertIsNone(rows[0].latest_stage)


class IssuesExtensionTest(unittest.TestCase):
    """Extended `get_issues` adds the highest review round any agent
    run for the issue reached and how many of those runs exited
    non-zero. Both are zero-defaulted so old 10-tuple fixtures still
    round-trip."""

    def test_extended_columns_round_trip(self) -> None:
        # 13-tuple: repo / issue / events / first / last / latest_stage
        # / agent_exits / cost / input / output / max_review_round /
        # failed_agent_runs / max_retry_count. The trailing
        # `max_retry_count` powers the redesigned "Retries" column
        # in the "Most expensive issues" table.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _GROUP_BY_PAIR: [
                (_REPO_SHORT, 7, 8, _EVENT_TS, _EVENT_TS, _STAGE_IMPLEMENTING,
                 5, 0.55, 800, 400, 3, 2, 4),
            ],
        }
        rows = analytics_read.get_issues(connect=conn.as_connect)
        self.assertEqual(rows[0].max_review_round, 3)
        self.assertEqual(rows[0].failed_agent_runs, 2)
        self.assertEqual(rows[0].max_retry_count, 4)
        sql, _ = conn.first_query
        self.assertIn("MAX(review_round)", sql)
        self.assertIn("MAX(retry_count)", sql)
        self.assertIn("failed_agent_runs", sql)

    def test_legacy_ten_tuple_fixture_round_trips(self) -> None:
        # A 10-tuple fixture pre-dates the review-round / retry
        # columns; `max_review_round` and `max_retry_count` default
        # to None and `failed_agent_runs` to zero.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _GROUP_BY_PAIR: [
                (_REPO_SHORT, 7, 1, _EVENT_TS, _EVENT_TS, None, 0, None, 0, 0),
            ],
        }
        rows = analytics_read.get_issues(connect=conn.as_connect)
        self.assertIsNone(rows[0].max_review_round)
        self.assertEqual(rows[0].failed_agent_runs, 0)
        self.assertIsNone(rows[0].max_retry_count)

    def test_default_sort_by_last_seen(self) -> None:
        # Backwards compatibility: the default `sort_by` keeps the
        # historical `ORDER BY last_seen DESC` so callers that pre-
        # date the redesigned cost-ordered top-issues read keep
        # surfacing the most recently active issues first.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {_GROUP_BY_PAIR: []}
        analytics_read.get_issues(connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn("ORDER BY last_seen DESC", sql)
        self.assertNotIn("SUM(cost_usd) DESC", sql)

    def test_sort_by_cost_orders_by_total_cost_desc(self) -> None:
        # Cost-ordered mode powers the redesigned "Most expensive
        # issues" table -- ordering in-Python after a `last_seen`-
        # bounded `LIMIT 200` would silently drop older high-cost
        # issues outside the truncated set, so the SQL must rank by
        # `SUM(cost_usd) DESC` directly.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {_GROUP_BY_PAIR: []}
        analytics_read.get_issues(
            sort_by=analytics_read.SORT_BY_COST,
            connect=conn.as_connect,
        )
        sql, _ = conn.first_query
        self.assertIn("ORDER BY SUM(cost_usd) DESC NULLS LAST", sql)
        # Secondary `last_seen DESC` keeps ties deterministic.
        self.assertIn("last_seen DESC", sql)

    def test_unknown_sort_by_raises(self) -> None:
        # A typo never silently degrades to the default ordering --
        # so a future caller is forced to pick a known mode.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        with self.assertRaises(ValueError):
            analytics_read.get_issues(
                sort_by="not-a-mode",
                connect=conn.as_connect,
            )
        # Argument validation runs before the DB connect, so the fake
        # cursor never receives the SQL.
        self.assertEqual(conn.executed, [])


class IssueEventsTest(unittest.TestCase):

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_issue_events(
                repo=_REPO_SHORT, issue=1,
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_returns_rows_for_repo_issue(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        ts_later = datetime(_YEAR, 5, 25, 12, 5, tzinfo=timezone.utc)
        conn.rows_for = {
            "WHERE repo = %s AND issue = %s": [
                (_NOON_TS, _STAGE_ENTER, _STAGE_IMPLEMENTING, None, None,
                 None, None, None, None),
                (ts_later, _AGENT_EXIT, _STAGE_IMPLEMENTING, 42.0, None,
                 _AGENT_ROLE_DEV, _BACKEND_CLAUDE, 0, 0.05),
            ],
        }
        rows = analytics_read.get_issue_events(
            repo=_REPO_SHORT, issue=7, connect=conn.as_connect,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].event, _STAGE_ENTER)
        self.assertEqual(rows[0].stage, _STAGE_IMPLEMENTING)
        self.assertEqual(rows[1].event, _AGENT_EXIT)
        self.assertEqual(rows[1].duration_s, 42.0)
        self.assertEqual(rows[1].backend, _BACKEND_CLAUDE)
        self.assertEqual(rows[1].cost_usd, 0.05)
        # Parameterised, not interpolated.
        _, query_params = conn.first_query
        self.assertEqual(query_params, (_REPO_SHORT, 7))
