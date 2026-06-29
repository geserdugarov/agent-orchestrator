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


class StageEventBreakdownTest(unittest.TestCase):

    def test_stage_breakdown_empty_when_db_url_unset(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_stage_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_stage_breakdown_handles_null_avg(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # Rollup-backed: the SQL recovers `AVG(duration_s)` as
        # `SUM(duration_s_sum) / NULLIF(SUM(duration_s_count), 0)`.
        # The fake fixture pre-computes that ratio so the reader's
        # NULL handling still rides through.
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("implementing", 20, 12.5),
                ("validating", 10, None),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].stage, "implementing")
        self.assertEqual(rows[0].count, 20)
        self.assertEqual(rows[0].avg_duration_s, 12.5)
        self.assertIsNone(rows[1].avg_duration_s)
        sql = conn.executed[0][0]
        # `IS NOT NULL` guard on stage is still present.
        self.assertIn("stage IS NOT NULL", sql)
        # Weighted-duration recovery from the rollup, not a
        # base-table `AVG(duration_s)`.
        self.assertIn("SUM(duration_s_sum)", sql)
        self.assertIn("NULLIF(SUM(duration_s_count), 0)", sql)

    def test_event_breakdown_returns_rows(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "GROUP BY event": [("agent_exit", 5), ("stage_enter", 3)],
        }
        rows = analytics_read.get_event_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].event, "agent_exit")
        self.assertEqual(rows[0].count, 5)
        self.assertEqual(rows[1].event, "stage_enter")
        self.assertEqual(rows[1].count, 3)


class StageBreakdownExtensionTest(unittest.TestCase):
    """Extended `get_stage_breakdown` rolls up cost / token totals
    plus an agent-run subset count per stage so the redesigned "Cost
    by workflow stage" panel can label its sub-line as "runs"
    against the per-stage cost. The cost is further split into
    cache_cost_usd / no_cache_cost_usd so the panel can stack
    cache vs no-cache spend per stage."""

    def test_rolls_up_cost_tokens_runs_and_cache_split(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 9-tuple shape: stage / events / avg_dur / cost / input /
        # output / agent-run subset / cache_cost / no_cache_cost.
        # The reader reads from the daily rollup so the SQL aggregates
        # the rollup's `total_*` columns instead of the raw events
        # table; the cache split is prorated per rollup row by
        # token share so cache + no-cache sums back to the stage's
        # total cost.
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("implementing", 20, 12.5, 0.50, 2000, 1500, 8, 0.30, 0.20),
                ("validating", 10, None, 0.10, 100, 200, 3, 0.04, 0.06),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].total_cost_usd, 0.50)
        self.assertEqual(rows[0].total_input_tokens, 2000)
        self.assertEqual(rows[0].total_output_tokens, 1500)
        self.assertEqual(rows[0].runs, 8)
        self.assertEqual(rows[0].cache_cost_usd, 0.30)
        self.assertEqual(rows[0].no_cache_cost_usd, 0.20)
        self.assertEqual(rows[1].total_cost_usd, 0.10)
        self.assertEqual(rows[1].runs, 3)
        self.assertEqual(rows[1].cache_cost_usd, 0.04)
        self.assertEqual(rows[1].no_cache_cost_usd, 0.06)
        sql, _ = conn.executed[0]
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

    def test_legacy_7tuple_fixture_round_trips(self) -> None:
        # Older fixtures still emit 7-tuple `(stage, count, avg_dur,
        # cost, in, out, runs)` rows without the cache split; the
        # reader defaults `cache_cost_usd` / `no_cache_cost_usd` to
        # zero so unrelated tests round-trip.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("implementing", 20, 12.5, 0.50, 2000, 1500, 8),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].runs, 8)
        self.assertEqual(rows[0].cache_cost_usd, 0.0)
        self.assertEqual(rows[0].no_cache_cost_usd, 0.0)

    def test_legacy_6tuple_fixture_round_trips(self) -> None:
        # Older fixtures still emit 6-tuple `(stage, count, avg_dur,
        # cost, in, out)` rows without the agent-run subset; the
        # reader defaults `runs` to zero so unrelated tests round-
        # trip.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("implementing", 20, 12.5, 0.50, 2000, 1500),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].runs, 0)
        self.assertEqual(rows[0].cache_cost_usd, 0.0)
        self.assertEqual(rows[0].no_cache_cost_usd, 0.0)


class RecentAgentExitsTest(unittest.TestCase):

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_recent_agent_exits(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_non_positive_limit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        connected = []
        result = analytics_read.get_recent_agent_exits(
            limit=0,
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result, [])

    def test_returns_rows_filtered_to_agent_exit(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        ts = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
        conn.rows_for = {
            "ORDER BY ts DESC LIMIT %s": [
                (
                    ts, "owner/r", 7, "implementing", "dev", "claude",
                    33.0, 0, False, 1, 0, 100, 200, 0.12, "cli",
                ),
            ],
        }
        rows = analytics_read.get_recent_agent_exits(
            limit=10, repo="owner/r", connect=_connector(conn),
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.ts, ts)
        self.assertEqual(row.repo, "owner/r")
        self.assertEqual(row.issue, 7)
        self.assertEqual(row.stage, "implementing")
        self.assertEqual(row.agent_role, "dev")
        self.assertEqual(row.backend, "claude")
        self.assertEqual(row.duration_s, 33.0)
        self.assertEqual(row.exit_code, 0)
        self.assertFalse(row.timed_out)
        self.assertEqual(row.cost_usd, 0.12)
        self.assertEqual(row.cost_source, "cli")
        # Query carries event='agent_exit' + repo filter + limit.
        sql, params = conn.executed[0]
        self.assertIn("event = %s", sql)
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(params, ("agent_exit", "owner/r", 10))


class IssuesOverviewTest(unittest.TestCase):
    """The dashboard's "issues" table: one row per `(repo, issue)`
    pair inside the window. Distinct from `get_issue_events` which
    drills into a single known issue."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        connected = []
        result = analytics_read.get_issues(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result, [])

    def test_non_positive_limit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        connected = []
        result = analytics_read.get_issues(
            limit=0,
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result, [])

    def test_groups_by_repo_issue_pair(self) -> None:
        # Two issues sharing the bare issue number 1 across two repos
        # must surface as two distinct rows. This is the dashboard
        # complement to `test_distinct_issues_counts_repo_issue_pairs`
        # in SummaryTest.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        t1 = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc)
        t4 = datetime(2026, 5, 26, 9, 30, tzinfo=timezone.utc)
        conn.rows_for = {
            "GROUP BY repo, issue": [
                (
                    "owner/b", 1, 3, t3, t4, "validating", 1,
                    0.42, 500, 300,
                ),
                (
                    "owner/a", 1, 5, t1, t2, "implementing", 2,
                    None, 0, 0,
                ),
            ],
        }
        rows = analytics_read.get_issues(connect=_connector(conn))
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0].repo, rows[0].issue), ("owner/b", 1))
        self.assertEqual((rows[1].repo, rows[1].issue), ("owner/a", 1))
        # Aggregates plumbed through positionally:
        self.assertEqual(rows[0].event_count, 3)
        self.assertEqual(rows[0].first_seen, t3)
        self.assertEqual(rows[0].last_seen, t4)
        self.assertEqual(rows[0].latest_stage, "validating")
        self.assertEqual(rows[0].agent_exits, 1)
        self.assertEqual(rows[0].total_cost_usd, 0.42)
        self.assertEqual(rows[0].total_input_tokens, 500)
        self.assertEqual(rows[0].total_output_tokens, 300)
        # None cost survives as None (not coerced to 0.0).
        self.assertIsNone(rows[1].total_cost_usd)
        # SQL shape: GROUP BY pair, ORDER BY last_seen DESC, LIMIT.
        sql, params = conn.executed[0]
        self.assertIn("GROUP BY repo, issue", sql)
        self.assertIn("ORDER BY last_seen DESC", sql)
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(params[-1], 100)

    def test_window_and_repo_params_bound(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 28, tzinfo=timezone.utc)
        analytics_read.get_issues(
            start=start, end=end, repo="owner/r", limit=25,
            connect=_connector(conn),
        )
        sql, params = conn.executed[0]
        self.assertIn("ts >= %s", sql)
        self.assertIn("ts < %s", sql)
        self.assertIn("repo = %s", sql)
        self.assertEqual(params, (start, end, "owner/r", 25))

    def test_null_latest_stage_survives(self) -> None:
        # `latest_stage` is None when no event for the issue in the
        # window carried a stage (e.g. only `agent_exit` rows whose
        # stage column happened to be null).
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        t = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
        conn.rows_for = {
            "GROUP BY repo, issue": [
                ("owner/r", 7, 1, t, t, None, 0, None, 0, 0),
            ],
        }
        rows = analytics_read.get_issues(connect=_connector(conn))
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
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        t = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
        conn.rows_for = {
            "GROUP BY repo, issue": [
                ("owner/r", 7, 8, t, t, "implementing", 5,
                 0.55, 800, 400, 3, 2, 4),
            ],
        }
        rows = analytics_read.get_issues(connect=_connector(conn))
        self.assertEqual(rows[0].max_review_round, 3)
        self.assertEqual(rows[0].failed_agent_runs, 2)
        self.assertEqual(rows[0].max_retry_count, 4)
        sql, _ = conn.executed[0]
        self.assertIn("MAX(review_round)", sql)
        self.assertIn("MAX(retry_count)", sql)
        self.assertIn("failed_agent_runs", sql)

    def test_legacy_10tuple_fixture_still_round_trips(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        t = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
        conn.rows_for = {
            "GROUP BY repo, issue": [
                ("owner/r", 7, 1, t, t, None, 0, None, 0, 0),
            ],
        }
        rows = analytics_read.get_issues(connect=_connector(conn))
        self.assertIsNone(rows[0].max_review_round)
        self.assertEqual(rows[0].failed_agent_runs, 0)
        self.assertIsNone(rows[0].max_retry_count)

    def test_default_sort_by_last_seen(self) -> None:
        # Backwards compatibility: the default `sort_by` keeps the
        # historical `ORDER BY last_seen DESC` so callers that pre-
        # date the redesigned cost-ordered top-issues read keep
        # surfacing the most recently active issues first.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"GROUP BY repo, issue": []}
        analytics_read.get_issues(connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("ORDER BY last_seen DESC", sql)
        self.assertNotIn("SUM(cost_usd) DESC", sql)

    def test_sort_by_cost_orders_by_total_cost_desc(self) -> None:
        # Cost-ordered mode powers the redesigned "Most expensive
        # issues" table -- ordering in-Python after a `last_seen`-
        # bounded `LIMIT 200` would silently drop older high-cost
        # issues outside the truncated set, so the SQL must rank by
        # `SUM(cost_usd) DESC` directly.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"GROUP BY repo, issue": []}
        analytics_read.get_issues(
            sort_by=analytics_read.SORT_BY_COST,
            connect=_connector(conn),
        )
        sql, _ = conn.executed[0]
        self.assertIn("ORDER BY SUM(cost_usd) DESC NULLS LAST", sql)
        # Secondary `last_seen DESC` keeps ties deterministic.
        self.assertIn("last_seen DESC", sql)

    def test_unknown_sort_by_raises(self) -> None:
        # A typo never silently degrades to the default ordering --
        # so a future caller is forced to pick a known mode.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        with self.assertRaises(ValueError):
            analytics_read.get_issues(
                sort_by="not-a-mode",
                connect=_connector(conn),
            )
        # Argument validation runs before the DB connect, so the fake
        # cursor never receives the SQL.
        self.assertEqual(conn.executed, [])


class IssueEventsTest(unittest.TestCase):

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_issue_events(
                repo="owner/r", issue=1,
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_returns_rows_for_repo_issue(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        ts1 = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 5, 25, 12, 5, tzinfo=timezone.utc)
        conn.rows_for = {
            "WHERE repo = %s AND issue = %s": [
                (ts1, "stage_enter", "implementing", None, None,
                 None, None, None, None),
                (ts2, "agent_exit", "implementing", 42.0, None,
                 "dev", "claude", 0, 0.05),
            ],
        }
        rows = analytics_read.get_issue_events(
            repo="owner/r", issue=7, connect=_connector(conn),
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].event, "stage_enter")
        self.assertEqual(rows[0].stage, "implementing")
        self.assertEqual(rows[1].event, "agent_exit")
        self.assertEqual(rows[1].duration_s, 42.0)
        self.assertEqual(rows[1].backend, "claude")
        self.assertEqual(rows[1].cost_usd, 0.05)
        # Parameterised, not interpolated.
        sql, params = conn.executed[0]
        self.assertEqual(params, ("owner/r", 7))
