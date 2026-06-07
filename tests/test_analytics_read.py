# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone
from typing import Any, Callable
from unittest.mock import patch


def _hermetic_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
    }
    if extra:
        env.update(extra)
    return env


def _reload(env: dict[str, str] | None = None):
    """Reload `orchestrator.config`, `orchestrator.analytics`, and
    `orchestrator.analytics.read` against the given hermetic env,
    mirroring `test_analytics_sync`.

    The analytics package owns the `ANALYTICS_DB_URL` parsing now,
    and `analytics.read` reads it off the parent package at call
    time, so the parent must be popped alongside `read` for the test
    env to land. `config` is popped too so `analytics.__init__`'s
    `from .. import config` reloads against the patched env (it
    still reads `LOG_DIR` for the JSONL default).
    """
    with patch.dict(os.environ, _hermetic_env(env), clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.analytics.read", None)
        sys.modules.pop("orchestrator.analytics", None)
        import orchestrator.analytics as analytics
        from orchestrator.analytics import read as analytics_read
        return analytics, analytics_read


class _FakeCursor:
    """Records every (sql, params) executed and returns canned rows.

    Implemented as a context manager so the production
    `with conn.cursor() as cur:` block works unchanged. `rows_for`
    is a dict mapping a substring of the SQL to the rows the cursor
    should return -- tests register expected query shapes by their
    most distinctive keyword (`COUNT(*) AS total_events`,
    `date_trunc`, etc.) so a refactor of unrelated SQL doesn't
    accidentally trip the assertion.
    """

    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn
        self._next_rows: list[tuple] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        self._conn.executed.append((sql, tuple(params)))
        if self._conn.raise_on_execute is not None:
            raise self._conn.raise_on_execute
        self._next_rows = []
        for needle, rows in self._conn.rows_for.items():
            if needle in sql:
                self._next_rows = list(rows)
                break

    def fetchall(self) -> list[tuple]:
        return list(self._next_rows)


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.rows_for: dict[str, list[tuple]] = {}
        self.raise_on_execute: Exception | None = None
        self.close_called = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        self.close_called += 1


def _connector(conn: _FakeConnection):
    """Build a `connect(db_url) -> conn` factory that always returns
    the same fake connection, so tests can inspect it after.
    """

    def _connect(_url: str) -> _FakeConnection:
        return conn

    return _connect


class FilterOptionsTest(unittest.TestCase):
    """Filter dropdown population: distinct sorted strings per column,
    empty when nothing is configured."""

    def test_returns_empty_when_db_url_unset(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        connected = []
        result = analytics_read.get_filter_options(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result, analytics_read.FilterOptions())

    def test_sentinel_off_is_unset(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "off"})
        result = analytics_read.get_filter_options(
            connect=lambda url: _FakeConnection(),
        )
        self.assertEqual(result, analytics_read.FilterOptions())

    def test_collects_distinct_values_per_column(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # Layer 3 collapses the five SELECT DISTINCTs into one
        # unioned query; rows are tagged with the column they belong
        # to and the reader buckets them in Python. Fixtures emit
        # values in arbitrary order so the in-Python sort that
        # preserves the previous ascending semantics is exercised.
        conn.rows_for = {
            "UNION SELECT 'event' AS dim": [
                ("repo", "owner/b"), ("repo", "owner/a"),
                ("event", "stage_enter"), ("event", "agent_exit"),
                ("stage", "validating"), ("stage", "implementing"),
                ("backend", "codex"), ("backend", "claude"),
                ("agent_role", "review"), ("agent_role", "dev"),
            ],
        }
        result = analytics_read.get_filter_options(connect=_connector(conn))
        self.assertEqual(result.repos, ("owner/a", "owner/b"))
        self.assertEqual(result.events, ("agent_exit", "stage_enter"))
        self.assertEqual(result.stages, ("implementing", "validating"))
        self.assertEqual(result.backends, ("claude", "codex"))
        self.assertEqual(result.agent_roles, ("dev", "review"))
        # One unioned query covers all five columns.
        self.assertEqual(len(conn.executed), 1)
        sql, _ = conn.executed[0]
        # Each leg excludes NULLs via its own WHERE clause -- the
        # union keeps the partial-scan plan per column.
        self.assertEqual(sql.count("IS NOT NULL"), 5)
        # Connection is closed once because there is now one query.
        self.assertEqual(conn.close_called, 1)

    def test_drops_null_rows(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # A row whose `value` is NULL would not be returned by the
        # SQL (each leg filters `IS NOT NULL`), but the Python
        # bucketer also guards against NULL so a driver that decides
        # to surface a stray NULL never blows up the reader.
        conn.rows_for = {
            "UNION SELECT 'event' AS dim": [
                ("repo", "owner/a"),
                ("repo", None),
                ("repo", "owner/b"),
            ],
        }
        result = analytics_read.get_filter_options(connect=_connector(conn))
        self.assertEqual(result.repos, ("owner/a", "owner/b"))

    def test_empty_rows_yield_empty_filter_options(self) -> None:
        # An empty table (or empty post-filter union) returns the
        # zero-valued `FilterOptions` rather than raising. Mirrors
        # the previous per-column path's empty-result behavior.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {}
        result = analytics_read.get_filter_options(connect=_connector(conn))
        self.assertEqual(result, analytics_read.FilterOptions())
        self.assertEqual(len(conn.executed), 1)

    def test_unknown_dim_rows_are_ignored(self) -> None:
        # A row whose `dim` is not one of the five known columns
        # (a forward-compat scenario where the SQL gains a leg the
        # reader has not learned about yet) is dropped rather than
        # routed to a stray bucket. Keeps the bucket dict bounded
        # to the dataclass's documented fields.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "UNION SELECT 'event' AS dim": [
                ("repo", "owner/a"),
                ("model", "claude-4-7"),
            ],
        }
        result = analytics_read.get_filter_options(connect=_connector(conn))
        self.assertEqual(result.repos, ("owner/a",))


class SummaryTest(unittest.TestCase):
    """Date-bounded aggregate counts plus per-event / per-stage
    breakdowns. Empty results give a zero-valued Summary, not None."""

    def test_returns_zero_summary_when_db_url_unset(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        connected = []
        result = analytics_read.get_summary(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result, analytics_read.Summary())

    def test_empty_rows_yield_zero_summary(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # No rows from the unioned SELECT (a fake that emits nothing).
        conn.rows_for = {}
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result, analytics_read.Summary())

    def test_aggregates_and_breakdowns(self) -> None:
        # Layer 3 collapses totals + by_event + by_stage into one
        # UNION-ALL'd query keyed by a `kind` discriminator. Each
        # row carries the 13-column shape; the by_event / by_stage
        # rows only populate `kind`, `label`, and `count_val`, with
        # trailing NULLs that the reader ignores. The fixture emits
        # the breakdown pairs in arbitrary order so the in-Python
        # `COUNT DESC, label ASC` sort that preserves the previous
        # SQL ordering is exercised.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 42, 10, 2, 1.234, 100, 200, 0, 0, 0, 0, 0),
                ("e", "agent_exit", 12, None, None, None, None, None,
                 None, None, None, None, None),
                ("e", "stage_enter", 30, None, None, None, None, None,
                 None, None, None, None, None),
                ("s", "validating", 10, None, None, None, None, None,
                 None, None, None, None, None),
                ("s", "implementing", 20, None, None, None, None, None,
                 None, None, None, None, None),
            ],
        }
        result = analytics_read.get_summary(
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 5, 28, tzinfo=timezone.utc),
            repo="owner/repo",
            connect=_connector(conn),
        )
        self.assertEqual(result.total_events, 42)
        self.assertEqual(result.distinct_issues, 10)
        self.assertEqual(result.distinct_repos, 2)
        self.assertEqual(result.total_cost_usd, 1.234)
        self.assertEqual(result.total_input_tokens, 100)
        self.assertEqual(result.total_output_tokens, 200)
        # Insertion order must match `c DESC, label ASC` so the
        # dashboard's iteration order does not depend on which UNION
        # plan Postgres picked.
        self.assertEqual(
            list(result.by_event.items()),
            [("stage_enter", 30), ("agent_exit", 12)],
        )
        self.assertEqual(
            list(result.by_stage.items()),
            [("implementing", 20), ("validating", 10)],
        )
        # And the whole result came from a single round-trip.
        self.assertEqual(len(conn.executed), 1)

    def test_window_and_repo_params_bound(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 28, tzinfo=timezone.utc)
        analytics_read.get_summary(
            start=start, end=end, repo="owner/r",
            connect=_connector(conn),
        )
        # The single combined SQL applies the filter once in the CTE
        # and the totals / breakdown branches inherit it from `win`.
        # The Layer 4 cutover swapped the events-table scan for the
        # daily rollup, so the window predicate is now on `day`
        # (the rollup's UTC-bound aggregate key) and the parameters
        # carry the `.date()` projection of the input timestamps.
        self.assertEqual(len(conn.executed), 1)
        sql, params = conn.executed[0]
        self.assertIn("WITH win AS", sql)
        self.assertIn("FROM analytics_daily_rollup", sql)
        self.assertIn("day >= %s", sql)
        self.assertIn("day < %s", sql)
        self.assertIn("repo = %s", sql)
        self.assertEqual(params[:3], (start.date(), end.date(), "owner/r"))

    def test_distinct_issues_counts_repo_issue_pairs(self) -> None:
        # GitHub issue numbers are only unique within a repo, so a
        # multi-repo window must count `(repo, issue)` pairs, not bare
        # `issue`. Otherwise `owner/a#1` and `owner/b#1` would collapse
        # into one and undercount activity. The fake here represents a
        # window that holds two distinct (repo, issue) pairs sharing
        # issue=1; the SQL must read `COUNT(DISTINCT (repo, issue))` so
        # the fake aggregate reflecting `2` round-trips into the
        # `distinct_issues` field.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 4, 2, 2, 0.0, 0, 0, 0, 0, 0, 0, 0),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.distinct_issues, 2)
        sql, _ = conn.executed[0]
        self.assertIn("COUNT(DISTINCT (repo, issue))", sql)


class TimeSeriesTest(unittest.TestCase):

    def test_unset_db_url_returns_empty_list(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_time_series(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_groups_by_day_and_event(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # Reads the daily rollup -- the rollup's `day` column is
        # the GROUP BY key, so the SQL no longer needs a
        # `date_trunc('day', ts)` expression.
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                (date(2026, 5, 25), "stage_enter", 5),
                (date(2026, 5, 25), "agent_exit", 2),
                (date(2026, 5, 26), "stage_enter", 7),
            ],
        }
        points = analytics_read.get_time_series(connect=_connector(conn))
        self.assertEqual(
            points,
            [
                analytics_read.TimeSeriesPoint(date(2026, 5, 25), "stage_enter", 5),
                analytics_read.TimeSeriesPoint(date(2026, 5, 25), "agent_exit", 2),
                analytics_read.TimeSeriesPoint(date(2026, 5, 26), "stage_enter", 7),
            ],
        )

    def test_datetime_day_normalised_to_date(self) -> None:
        # Some drivers return the `day` column as a timestamp even
        # when the underlying type is `date`; the read model
        # normalises so the dashboard sees `date`.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                (datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc), "x", 1),
            ],
        }
        points = analytics_read.get_time_series(connect=_connector(conn))
        self.assertEqual(points[0].day, date(2026, 5, 25))
        self.assertEqual(points[0].count, 1)


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


class DefaultDbUrlTest(unittest.TestCase):
    """When no `db_url` kwarg is passed, `analytics.ANALYTICS_DB_URL`
    is the default."""

    def test_config_url_used_when_kwarg_omitted(self) -> None:
        analytics, analytics_read = _reload(
            {"ANALYTICS_DB_URL": "postgresql://from-env/db"}
        )
        seen: list[str] = []

        def _capture_connect(url: str) -> _FakeConnection:
            seen.append(url)
            return _FakeConnection()

        analytics_read.get_filter_options(connect=_capture_connect)
        self.assertEqual(seen[0], "postgresql://from-env/db")
        self.assertEqual(analytics.ANALYTICS_DB_URL, "postgresql://from-env/db")

    def test_explicit_kwarg_overrides_config(self) -> None:
        _, analytics_read = _reload(
            {"ANALYTICS_DB_URL": "postgresql://from-env/db"}
        )
        seen: list[str] = []

        def _capture_connect(url: str) -> _FakeConnection:
            seen.append(url)
            return _FakeConnection()

        analytics_read.get_filter_options(
            db_url="postgresql://override/db",
            connect=_capture_connect,
        )
        self.assertEqual(seen[0], "postgresql://override/db")


class EventStageIssueFilterTest(unittest.TestCase):
    """The dashboard threads its event / stage / issue filters into
    every read so the rendered widgets move together. These tests
    cover the SQL the read model emits for the three cases
    `_build_window_where` distinguishes: ``None`` (no filter),
    non-empty sequence (parameterised ``IN``), and empty sequence
    (the dashboard's cleared-multiselect signal, which must
    short-circuit to no rows -- a previous implementation treated it
    as ``None`` and the dashboard inadvertently rendered the
    unfiltered window).
    """

    def test_events_in_clause_with_params(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_summary(
            events=["agent_exit", "stage_enter"],
            stages=["implementing"],
            connect=_connector(conn),
        )
        # Layer 3's combined SQL applies the filter once in the CTE;
        # the totals + breakdown branches inherit from it.
        self.assertEqual(len(conn.executed), 1)
        sql, params = conn.executed[0]
        self.assertIn("event IN (%s, %s)", sql)
        self.assertIn("stage IN (%s)", sql)
        self.assertIn("agent_exit", params)
        self.assertIn("stage_enter", params)
        self.assertIn("implementing", params)

    def test_empty_events_emits_false_predicate(self) -> None:
        # The dashboard's "cleared multiselect" case: an empty list
        # means "no rows match" rather than "no filter". The SQL
        # carries a tautologically-false predicate; the database
        # never returns any row.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_summary(events=[], connect=_connector(conn))
        for sql, _ in conn.executed:
            self.assertIn("FALSE", sql)

    def test_empty_stages_emits_false_predicate(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_time_series(stages=[], connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("FALSE", sql)

    def test_issue_filter_narrows_summary(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_summary(
            repo="owner/r", issue=42, connect=_connector(conn),
        )
        sql, params = conn.executed[0]
        self.assertIn("issue = %s", sql)
        self.assertIn(42, params)


class RecentAgentExitsFilterTest(unittest.TestCase):
    """The reviewer flagged that `get_recent_agent_exits` ignored
    the sidebar date window. The function now accepts `start`,
    `end`, `events`, `stages`, and `issue` so the recent-runs table
    narrows with the rest of the dashboard.
    """

    def test_date_window_threaded_into_where(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 28, tzinfo=timezone.utc)
        analytics_read.get_recent_agent_exits(
            limit=10, start=start, end=end, repo="owner/r",
            connect=_connector(conn),
        )
        sql, params = conn.executed[0]
        self.assertIn("ts >= %s", sql)
        self.assertIn("ts < %s", sql)
        self.assertEqual(params[1], start)
        self.assertEqual(params[2], end)
        self.assertEqual(params[3], "owner/r")
        self.assertEqual(params[-1], 10)

    def test_event_filter_excluding_agent_exit_short_circuits(self) -> None:
        # If the operator deselects `agent_exit` from the events
        # multiselect, the recent-runs widget logically has no rows
        # -- it is by definition about `agent_exit`. Short-circuit
        # without touching the DB.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_recent_agent_exits(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_event_filter_including_agent_exit_runs_query(self) -> None:
        # Selection includes `agent_exit`; the SQL still hard-AND's
        # `event = 'agent_exit'` and the function returns rows.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        ts = datetime(2026, 5, 25, tzinfo=timezone.utc)
        conn.rows_for = {
            "ORDER BY ts DESC LIMIT %s": [
                (ts, "owner/r", 7, "implementing", "dev", "claude",
                 33.0, 0, False, 1, 0, 100, 200, 0.12, "cli"),
            ],
        }
        rows = analytics_read.get_recent_agent_exits(
            events=["agent_exit", "stage_enter"], stages=["implementing"],
            connect=_connector(conn),
        )
        self.assertEqual(len(rows), 1)
        sql, _ = conn.executed[0]
        self.assertIn("event = %s", sql)
        self.assertIn("stage IN (%s)", sql)

    def test_empty_stage_filter_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_recent_agent_exits(
            stages=[], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])


class IssueEventsFilterTest(unittest.TestCase):
    """The drill-down accepts the same window / event / stage filters
    so the per-issue trace stays consistent with the dashboard above.
    """

    def test_window_and_events_threaded(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 28, tzinfo=timezone.utc)
        analytics_read.get_issue_events(
            repo="owner/r", issue=7,
            start=start, end=end, events=["agent_exit"],
            connect=_connector(conn),
        )
        sql, params = conn.executed[0]
        self.assertIn("ts >= %s", sql)
        self.assertIn("ts < %s", sql)
        self.assertIn("event IN (%s)", sql)
        self.assertEqual(params[0], "owner/r")
        self.assertEqual(params[1], 7)
        self.assertEqual(params[2], start)
        self.assertEqual(params[3], end)
        self.assertEqual(params[4], "agent_exit")

    def test_empty_events_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_issue_events(
            repo="owner/r", issue=7, events=[], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])


class DataExtentTest(unittest.TestCase):
    """`get_data_extent` answers "what date range does the data
    actually cover" so the sidebar date picker can default to a
    window that contains rows. Empty / unset cases yield the
    zero-valued `DataExtent` so the dashboard can branch on it."""

    def test_unset_db_url_returns_empty_extent(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        connected = []
        result = analytics_read.get_data_extent(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result, analytics_read.DataExtent())

    def test_empty_table_returns_null_extents(self) -> None:
        # Postgres' MIN/MAX on an empty table returns one row of two
        # NULLs; the read model surfaces that as `(None, None)`.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"data_min_ts": [(None, None)]}
        result = analytics_read.get_data_extent(connect=_connector(conn))
        self.assertIsNone(result.min_ts)
        self.assertIsNone(result.max_ts)

    def test_returns_min_and_max(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        t0 = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        conn.rows_for = {"data_min_ts": [(t0, t1)]}
        result = analytics_read.get_data_extent(connect=_connector(conn))
        self.assertEqual(result.min_ts, t0)
        self.assertEqual(result.max_ts, t1)
        sql, _ = conn.executed[0]
        self.assertIn("MIN(ts)", sql)
        self.assertIn("MAX(ts)", sql)


class SummaryAgentRunsExtensionTest(unittest.TestCase):
    """The summary totals SQL emits `total_agent_runs` and
    `failed_agent_runs` (scoped to `event = 'agent_exit'` rows
    inside the same window) so the dashboard's success-rate panel
    reads off the same query as the rest of the overview."""

    def test_totals_carry_agent_run_columns(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # Full 13-column totals row: kind / label / events / issues
        # / repos / cost / input / output / total runs / failed runs
        # / cache_read / cache_write / timed_out. Layer 3's combined
        # SQL keeps every aggregate column on the totals row; Layer 4
        # swaps the events-table scan for the daily rollup so the
        # aggregates are recovered from the rollup's pre-derived
        # `failed_count` (`exit_code IS NOT NULL AND exit_code <> 0`)
        # narrowed to `event = 'agent_exit'`.
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 50, 12, 3, 2.5, 100, 200, 15, 4, 0, 0, 0),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.total_agent_runs, 15)
        self.assertEqual(result.failed_agent_runs, 4)
        sql, _ = conn.executed[0]
        self.assertIn("total_agent_runs", sql)
        self.assertIn("failed_agent_runs", sql)
        # Failure subset constrains on `event = 'agent_exit'` so a
        # non-exit row that happened to carry a non-null exit code
        # never counts; the NULL-exit-code guard lives in the rollup
        # definition.
        self.assertIn("event = 'agent_exit'", sql)
        self.assertIn("FROM analytics_daily_rollup", sql)

    def test_short_totals_tuple_round_trips(self) -> None:
        # A fixture whose totals row is shorter than the full
        # 13-column shape (e.g. a pre-extension fake) defaults the
        # missing trailing columns to zero rather than raising on
        # the unpack. Mirrors the previous "legacy 6-tuple" guard.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                # kind / label / events / issues / repos / cost /
                # input / output -- no agent-run or cache columns.
                ("t", None, 4, 2, 2, 0.0, 0, 0),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.total_agent_runs, 0)
        self.assertEqual(result.failed_agent_runs, 0)
        self.assertEqual(result.total_cache_read_tokens, 0)
        self.assertEqual(result.total_cache_write_tokens, 0)
        self.assertEqual(result.timed_out_agent_runs, 0)

    def test_totals_carry_cache_token_columns(self) -> None:
        # The cache columns feed the redesigned "Total tokens" KPI
        # and sparkline -- matching the standalone mock's
        # `input + output + cache_read + cache_write` accounting.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 50, 12, 3, 2.5, 100, 200, 15, 4, 1200, 800, 0),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.total_cache_read_tokens, 1200)
        self.assertEqual(result.total_cache_write_tokens, 800)
        sql, _ = conn.executed[0]
        # The rollup carries cache-band tokens pre-summed per group
        # under the `total_cache_*` column names, so the reader sums
        # the rollup columns rather than the raw event columns.
        self.assertIn("SUM(total_cache_read_tokens)", sql)
        self.assertIn("SUM(total_cache_write_tokens)", sql)

    def test_totals_carry_timed_out_agent_runs(self) -> None:
        # Window-wide `timed_out` count so the reliability "Timeouts"
        # tile aggregates every timed-out run in the window -- not
        # just the latest N from `get_recent_agent_exits`. The SQL
        # filters on `timed_out = true` so NULL pre-flag rows never
        # count.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 50, 12, 3, 2.5, 100, 200, 15, 4, 1200, 800, 7),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.timed_out_agent_runs, 7)
        sql, _ = conn.executed[0]
        self.assertIn("timed_out", sql)
        self.assertIn("timed_out_agent_runs", sql)


class KpiPrevTest(unittest.TestCase):
    """Layer 3's `get_kpi_prev`: a single-query previous-window
    reader that returns only the cost / token / agent-run scalars the
    dashboard's KPI delta pills and cost-trend banner consume.
    Public return type is still `Summary` so existing call sites
    (`compute_insights`, the dashboard's `prev_summary` consumers)
    keep their shape; the unread fields stay at their dataclass
    defaults."""

    def test_returns_zero_summary_when_db_url_unset(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        connected: list[str] = []
        result = analytics_read.get_kpi_prev(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(result, analytics_read.Summary())

    def test_returns_zero_summary_for_empty_window(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {}
        result = analytics_read.get_kpi_prev(connect=_connector(conn))
        self.assertEqual(result, analytics_read.Summary())

    def test_scalars_round_trip(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 6-tuple: cost / input / output / cache_read / cache_write
        # / total_agent_runs -- exactly what the dashboard reads off
        # `prev_summary` for KPI deltas and the cost-trend banner.
        conn.rows_for = {
            "AS total_cost_usd": [(2.5, 1000, 500, 200, 100, 7)],
        }
        result = analytics_read.get_kpi_prev(connect=_connector(conn))
        self.assertEqual(result.total_cost_usd, 2.5)
        self.assertEqual(result.total_input_tokens, 1000)
        self.assertEqual(result.total_output_tokens, 500)
        self.assertEqual(result.total_cache_read_tokens, 200)
        self.assertEqual(result.total_cache_write_tokens, 100)
        self.assertEqual(result.total_agent_runs, 7)
        # The trimmed reader leaves the unread fields at their
        # dataclass defaults so existing `prev_summary` consumers
        # see zero rather than stale values.
        self.assertEqual(result.total_events, 0)
        self.assertEqual(result.distinct_issues, 0)
        self.assertEqual(result.distinct_repos, 0)
        self.assertEqual(result.failed_agent_runs, 0)
        self.assertEqual(result.timed_out_agent_runs, 0)
        self.assertEqual(result.by_event, {})
        self.assertEqual(result.by_stage, {})

    def test_window_and_filter_params_bound(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        start = datetime(2026, 4, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 1, tzinfo=timezone.utc)
        analytics_read.get_kpi_prev(
            start=start, end=end, repo="owner/r",
            events=["agent_exit"], stages=["implementing"],
            connect=_connector(conn),
        )
        # One round-trip; the rollup window predicate replaces the
        # base-table `ts >= / ts <` shape with `day >= / day <`,
        # but the previous-window read still narrows alongside the
        # current-window summary.
        self.assertEqual(len(conn.executed), 1)
        sql, params = conn.executed[0]
        self.assertIn("FROM analytics_daily_rollup", sql)
        self.assertIn("day >= %s", sql)
        self.assertIn("day < %s", sql)
        self.assertIn("repo = %s", sql)
        self.assertIn("event IN (%s)", sql)
        self.assertIn("stage IN (%s)", sql)
        self.assertEqual(params[:3], (start.date(), end.date(), "owner/r"))

    def test_empty_events_emits_false_predicate(self) -> None:
        # Mirrors `get_summary`'s cleared-multiselect semantics: an
        # empty events list means "no rows match" rather than "no
        # filter". The SQL carries a tautologically-false predicate
        # so the previous-window KPI strip drops to zero alongside
        # the current-window read.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_kpi_prev(events=[], connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("FALSE", sql)

    def test_does_not_emit_breakdown_or_distinct_counts(self) -> None:
        # The trimmed shape is the whole point: the SQL must NOT
        # carry the `GROUP BY` follow-ups or the
        # `COUNT(DISTINCT ...)`s that `get_summary` emits, otherwise
        # the previous-window read still pays the same cost it did
        # before Layer 3.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_kpi_prev(connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertNotIn("GROUP BY", sql)
        self.assertNotIn("COUNT(DISTINCT", sql)

    def test_short_row_round_trips(self) -> None:
        # A fake that pre-dates the agent-run column still returns a
        # valid `Summary` with the missing trailing column defaulted
        # to zero -- mirrors the `get_summary` legacy-tuple guard.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "AS total_cost_usd": [(1.0, 100, 200, 50, 25)],
        }
        result = analytics_read.get_kpi_prev(connect=_connector(conn))
        self.assertEqual(result.total_cost_usd, 1.0)
        self.assertEqual(result.total_agent_runs, 0)


class TimeSeriesAggregatesTest(unittest.TestCase):
    """Reshaped `get_time_series` carries per-(day, event) cost /
    token aggregates so the spend-over-time and tokens-over-time
    charts can pivot the same query."""

    def test_aggregates_round_trip(self) -> None:
        # 8-tuple: day / event / count / cost / input / output /
        # cache_read / cache_write. The cache columns feed the
        # redesigned hero chart's Cache band. After Layer 4 the
        # reader sums the rollup's pre-derived `total_*` columns
        # instead of the raw event-table columns.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                (
                    date(2026, 5, 25), "agent_exit",
                    3, 0.42, 1000, 500, 200, 100,
                ),
            ],
        }
        points = analytics_read.get_time_series(connect=_connector(conn))
        self.assertEqual(len(points), 1)
        p = points[0]
        self.assertEqual(p.count, 3)
        self.assertEqual(p.cost_usd, 0.42)
        self.assertEqual(p.input_tokens, 1000)
        self.assertEqual(p.output_tokens, 500)
        self.assertEqual(p.cache_read_tokens, 200)
        self.assertEqual(p.cache_write_tokens, 100)
        sql, _ = conn.executed[0]
        self.assertIn("SUM(total_cost_usd)", sql)
        self.assertIn("SUM(total_input_tokens)", sql)
        self.assertIn("SUM(total_output_tokens)", sql)
        self.assertIn("SUM(total_cache_read_tokens)", sql)
        self.assertIn("SUM(total_cache_write_tokens)", sql)

    def test_legacy_six_tuple_rows_default_cache_to_zero(self) -> None:
        # Older fixtures still emit 6-tuple `(day, event, count,
        # cost, in, out)` rows; the reader defaults the cache fields
        # to zero so unrelated tests round-trip.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                (date(2026, 5, 25), "agent_exit", 3, 0.42, 1000, 500),
            ],
        }
        p = analytics_read.get_time_series(connect=_connector(conn))[0]
        self.assertEqual(p.cache_read_tokens, 0)
        self.assertEqual(p.cache_write_tokens, 0)


class StageBreakdownExtensionTest(unittest.TestCase):
    """Extended `get_stage_breakdown` rolls up cost / token totals
    plus an agent-run subset count per stage so the redesigned "Cost
    by workflow stage" panel can label its sub-line as "runs"
    against the per-stage cost."""

    def test_rolls_up_cost_tokens_and_runs(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 7-tuple shape: stage / events / avg_dur / cost / input /
        # output / agent-run subset. The reader reads from the
        # daily rollup so the SQL aggregates the rollup's `total_*`
        # columns instead of the raw events table.
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("implementing", 20, 12.5, 0.50, 2000, 1500, 8),
                ("validating", 10, None, 0.10, 100, 200, 3),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].total_cost_usd, 0.50)
        self.assertEqual(rows[0].total_input_tokens, 2000)
        self.assertEqual(rows[0].total_output_tokens, 1500)
        self.assertEqual(rows[0].runs, 8)
        self.assertEqual(rows[1].total_cost_usd, 0.10)
        self.assertEqual(rows[1].runs, 3)
        sql, _ = conn.executed[0]
        self.assertIn("SUM(total_cost_usd)", sql)
        # Agent-run subset uses `event = 'agent_exit'`, scoped by
        # the same WHERE clause as the totals so the per-stage sub-
        # line lines up with the per-stage cost.
        self.assertIn("event = 'agent_exit'", sql)

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


class ReviewRoundBreakdownTest(unittest.TestCase):
    """`get_review_round_breakdown` reads from `analytics_agent_runs`
    so the agent-run filter contract (no `event` column in the view)
    is encoded as a Python-side short-circuit on `_agent_event_excluded`."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_review_round_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_event_filter_excluding_agent_exit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_review_round_breakdown(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_review_round_breakdown(
            events=[], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_query_against_view_and_buckets_round_trip(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 12-tuple rows carry the role + cache split the new chart
        # consumes: (bucket, runs, failed, cost, dev_runs, rev_runs,
        # dev_cost, rev_cost, dev_cache, dev_no_cache, rev_cache,
        # rev_no_cache).
        conn.rows_for = {
            "analytics_agent_runs": [
                ("0", 12, 1, 40.0, 7, 5, 28.0, 12.0, 20.0, 8.0, 9.0, 3.0),
                ("1", 8, 2, 25.0, 4, 4, 10.0, 15.0, 7.0, 3.0, 11.0, 4.0),
                ("3-5", 4, 4, 18.0, 1, 3, 5.0, 13.0, 5.0, 0.0, 13.0, 0.0),
                ("unknown", 1, 0, 0.0, 1, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            ],
        }
        rows = analytics_read.get_review_round_breakdown(
            connect=_connector(conn),
        )
        self.assertEqual([r.bucket for r in rows], ["0", "1", "3-5", "unknown"])
        self.assertEqual([r.runs for r in rows], [12, 8, 4, 1])
        self.assertEqual([r.failed for r in rows], [1, 2, 4, 0])
        # `total_cost_usd` powers the redesigned "Cost by review round"
        # chart in `orchestrator.dashboard_charts.cost_by_review_round`
        # and the "Rework share" KPI tile.
        self.assertEqual(
            [r.total_cost_usd for r in rows],
            [40.0, 25.0, 18.0, 0.0],
        )
        self.assertEqual([r.developer_runs for r in rows], [7, 4, 1, 1])
        self.assertEqual([r.reviewer_runs for r in rows], [5, 4, 3, 0])
        self.assertEqual(
            [r.developer_cost_usd for r in rows],
            [28.0, 10.0, 5.0, 0.0],
        )
        self.assertEqual(
            [r.reviewer_cost_usd for r in rows],
            [12.0, 15.0, 13.0, 0.0],
        )
        # Cache vs no-cache split per role -- the chart stacks these
        # so cache_cost + no_cache_cost must equal the role's total.
        self.assertEqual(
            [r.developer_cache_cost_usd for r in rows],
            [20.0, 7.0, 5.0, 0.0],
        )
        self.assertEqual(
            [r.developer_no_cache_cost_usd for r in rows],
            [8.0, 3.0, 0.0, 0.0],
        )
        self.assertEqual(
            [r.reviewer_cache_cost_usd for r in rows],
            [9.0, 11.0, 13.0, 0.0],
        )
        self.assertEqual(
            [r.reviewer_no_cache_cost_usd for r in rows],
            [3.0, 4.0, 0.0, 0.0],
        )
        sql, _ = conn.executed[0]
        # Reads from the view, not the base table, and the view has
        # no `event` column so no `event IN (...)` clause is emitted.
        self.assertIn("FROM analytics_agent_runs", sql)
        self.assertIn("SUM(cost_usd)", sql)
        self.assertIn("agent_role IN ('developer', 'reviewer')", sql)
        self.assertIn("agent_role = 'developer'", sql)
        self.assertIn("agent_role = 'reviewer'", sql)
        self.assertIn("stage = 'implementing' THEN '0'", sql)
        self.assertNotIn("event IN", sql)
        # The cache / no-cache split is proportional: each run's cost
        # is weighted by the cache-token share of its billable token
        # volume. Codex `cached_tokens` is already a subset of
        # `input_tokens`, so it appears in the numerator only -- not
        # the denominator -- to avoid double-counting.
        self.assertIn("cached_tokens", sql)
        self.assertIn("cache_read_tokens", sql)
        self.assertIn("cache_write_tokens", sql)
        self.assertIn("developer_cache_cost_usd", sql)
        self.assertIn("developer_no_cache_cost_usd", sql)
        self.assertIn("reviewer_cache_cost_usd", sql)
        self.assertIn("reviewer_no_cache_cost_usd", sql)

    def test_legacy_three_tuple_rows_default_cost_to_zero(self) -> None:
        # Older fixtures still emit 3-tuple `(bucket, runs, failed)` rows
        # without the cost / role / cache rollups; the reader defaults
        # those values to zero so unrelated tests keep round-tripping.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"analytics_agent_runs": [("0", 3, 0)]}
        rows = analytics_read.get_review_round_breakdown(
            connect=_connector(conn),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].total_cost_usd, 0.0)
        self.assertEqual(rows[0].developer_cost_usd, 0.0)
        self.assertEqual(rows[0].reviewer_cost_usd, 0.0)
        self.assertEqual(rows[0].developer_cache_cost_usd, 0.0)
        self.assertEqual(rows[0].developer_no_cache_cost_usd, 0.0)
        self.assertEqual(rows[0].reviewer_cache_cost_usd, 0.0)
        self.assertEqual(rows[0].reviewer_no_cache_cost_usd, 0.0)

    def test_explicit_agent_exit_runs_query(self) -> None:
        # An events list that includes agent_exit must NOT short-circuit
        # -- the operator still wants to see the agent runs view.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"analytics_agent_runs": [("1", 3, 0, 5.0)]}
        rows = analytics_read.get_review_round_breakdown(
            events=["agent_exit", "stage_enter"],
            connect=_connector(conn),
        )
        self.assertEqual(len(rows), 1)


class BackendDailyTokensTest(unittest.TestCase):
    """`get_backend_daily_tokens` powers the redesigned dashboard's
    "By backend" hero toggle. It must read from the view, honor the
    agent-run event-filter short-circuit, and aggregate tokens across
    every agent run in the window (not a `LIMIT`-capped subset).
    """

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_backend_daily_tokens(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_event_filter_excluding_agent_exit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_backend_daily_tokens(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_backend_daily_tokens(
            events=[], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_reads_view_and_aggregates_per_day_per_backend(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "analytics_agent_runs": [
                (date(2026, 5, 1), "claude", 12_000),
                (date(2026, 5, 1), "codex", 4_500),
                (date(2026, 5, 2), "claude", 8_000),
            ],
        }
        rows = analytics_read.get_backend_daily_tokens(
            connect=_connector(conn),
        )
        self.assertEqual(
            [(r.day, r.backend, r.total_tokens) for r in rows],
            [
                (date(2026, 5, 1), "claude", 12_000),
                (date(2026, 5, 1), "codex", 4_500),
                (date(2026, 5, 2), "claude", 8_000),
            ],
        )
        sql, _ = conn.executed[0]
        # Reads from the view -- so the agent-run filter contract
        # (no `event IN` clause) holds -- and groups by both day and
        # backend so the dashboard can build a per-day stack without
        # post-processing. Token total includes the cache band so
        # the backend stack matches the standalone mock's
        # `input + output + cache_read + cache_write` accounting.
        self.assertIn("FROM analytics_agent_runs", sql)
        self.assertNotIn("event IN", sql)
        self.assertIn("GROUP BY day, backend_label", sql)
        for col in (
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens",
        ):
            self.assertIn(col, sql)

    def test_null_backend_buckets_under_unknown(self) -> None:
        # `COALESCE(backend, 'unknown')` matches how
        # `get_backend_efficiency` surfaces NULL-backend rows.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "analytics_agent_runs": [
                (date(2026, 5, 1), "unknown", 1_000),
            ],
        }
        rows = analytics_read.get_backend_daily_tokens(
            connect=_connector(conn),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].backend, "unknown")


class BackendEfficiencyTest(unittest.TestCase):
    """`get_backend_efficiency` aggregates the agent_runs view by
    backend and exposes failure / cost / token rollups."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_backend_efficiency(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_event_filter_excluding_agent_exit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_backend_efficiency(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_aggregates_round_trip(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 9-tuple: backend / runs / failed / avg_dur / cost /
        # input_tokens / output_tokens / cache_read / cache_write.
        # After Layer 4 the reader reads from the daily rollup
        # (with `event = 'agent_exit'` pinned to match the prior
        # view's filter); the fake fixture pre-computes the
        # weighted average so the reader's NULL handling still
        # rides through.
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("claude", 20, 1, 35.0, 1.20, 5000, 4000, 1500, 800),
                ("codex", 10, 3, None, 0.40, 1000, 2000, 0, 0),
                ("unknown", 1, 0, None, 0.0, 0, 0, 0, 0),
            ],
        }
        rows = analytics_read.get_backend_efficiency(connect=_connector(conn))
        self.assertEqual([r.backend for r in rows], ["claude", "codex", "unknown"])
        self.assertEqual(rows[0].runs, 20)
        self.assertEqual(rows[0].failed, 1)
        self.assertEqual(rows[0].avg_duration_s, 35.0)
        self.assertEqual(rows[0].total_cost_usd, 1.20)
        # Cache columns feed the per-backend "cost / 1M tok" tile
        # alongside input + output so the denominator matches the
        # standalone mock's total-token accounting.
        self.assertEqual(rows[0].total_cache_read_tokens, 1500)
        self.assertEqual(rows[0].total_cache_write_tokens, 800)
        # NULL avg duration preserved so the dashboard can hide the
        # column rather than show a misleading zero.
        self.assertIsNone(rows[1].avg_duration_s)
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_daily_rollup", sql)
        # The rollup carries an `event` column, so the cutover
        # query pins `event = 'agent_exit'` directly rather than
        # the view's implicit filter.
        self.assertIn("event = 'agent_exit'", sql)
        self.assertIn("COALESCE(backend, 'unknown')", sql)
        self.assertIn("SUM(total_cache_read_tokens)", sql)
        self.assertIn("SUM(total_cache_write_tokens)", sql)
        # Weighted-duration recovery from the rollup, not
        # `AVG(duration_s)` over the raw events table.
        self.assertIn("SUM(duration_s_sum)", sql)
        self.assertIn("NULLIF(SUM(duration_s_count), 0)", sql)

    def test_legacy_7tuple_fixture_defaults_cache_to_zero(self) -> None:
        # Older 7-tuple `(backend, runs, failed, avg_dur, cost, in,
        # out)` rows still round-trip with zero cache tokens so
        # unrelated tests keep working.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("claude", 5, 0, 10.0, 0.20, 1000, 500),
            ],
        }
        rows = analytics_read.get_backend_efficiency(connect=_connector(conn))
        self.assertEqual(rows[0].total_cache_read_tokens, 0)
        self.assertEqual(rows[0].total_cache_write_tokens, 0)


class RepoBreakdownTest(unittest.TestCase):
    """`get_repo_breakdown` reads the base table so the standard
    event/stage/date/repo/issue filter shape applies (no agent_runs
    short-circuit)."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_repo_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_per_repo_rows(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "GROUP BY repo": [
                ("owner/a", 5, 30, 4, 0.50),
                ("owner/b", 2, 10, 1, 0.10),
            ],
        }
        rows = analytics_read.get_repo_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].repo, "owner/a")
        self.assertEqual(rows[0].issues, 5)
        self.assertEqual(rows[0].events, 30)
        self.assertEqual(rows[0].agent_exits, 4)
        self.assertEqual(rows[0].total_cost_usd, 0.50)
        sql, _ = conn.executed[0]
        # GROUP BY repo with distinct issue count per row -- safe
        # because rollup rows are already scoped to one repo per
        # bucket and the rollup key carries `issue`.
        self.assertIn("COUNT(DISTINCT issue)", sql)
        self.assertIn("FROM analytics_daily_rollup", sql)

    def test_event_filter_threaded(self) -> None:
        # `get_repo_breakdown` honors the standard event filter
        # because it reads the base table (which carries an `event`
        # column). Cleared multiselect -> FALSE predicate.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_repo_breakdown(
            events=[], connect=_connector(conn),
        )
        sql, _ = conn.executed[0]
        self.assertIn("FALSE", sql)


class CostCoverageTest(unittest.TestCase):
    """`get_cost_coverage` MUST keep `unknown-price` visible -- it is
    the maintenance signal for the pricing table in
    `orchestrator.usage`. Distinct from rows whose `cost_source` is
    NULL, which bucket under the generic `"unknown"`."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_cost_coverage(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_event_filter_excluding_agent_exit_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_cost_coverage(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_unknown_price_preserved_verbatim(self) -> None:
        # The `unknown-price` slice surfaces with that exact label --
        # NEVER collapsed into "unknown" -- so the operator can see
        # which runs the parser could not price. The third tuple
        # column is the per-`cost_source` token rollup that feeds
        # the redesigned token-share coverage bar.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "analytics_agent_runs": [
                ("reported", 20, 800_000),
                ("estimated", 5, 100_000),
                ("unknown-price", 3, 60_000),
                ("no-usage", 2, 20_000),
                ("unknown", 1, 5_000),
            ],
        }
        rows = analytics_read.get_cost_coverage(connect=_connector(conn))
        labels = [r.cost_source for r in rows]
        self.assertIn("unknown-price", labels)
        # Make sure we did not silently fold it into "unknown".
        self.assertEqual(
            sum(1 for r in rows if r.cost_source == "unknown-price"), 1,
        )
        self.assertEqual(
            sum(1 for r in rows if r.cost_source == "unknown"), 1,
        )
        # Per-source token volume rolls up alongside the run count.
        by_source = {r.cost_source: r for r in rows}
        self.assertEqual(by_source["reported"].total_tokens, 800_000)
        self.assertEqual(by_source["unknown-price"].total_tokens, 60_000)
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_agent_runs", sql)
        # NULL cost_source rows bucket under "unknown" via COALESCE,
        # but the verbatim "unknown-price" string is untouched.
        self.assertIn("COALESCE(cost_source, 'unknown')", sql)
        # SQL totals input + output + cache_read + cache_write so the
        # token share matches the standalone mock's accounting.
        for col in (
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens",
        ):
            self.assertIn(col, sql)

    def test_legacy_two_tuple_rows_default_tokens_to_zero(self) -> None:
        # Older fixtures still emit 2-tuple `(cost_source, runs)`
        # rows; the reader defaults `total_tokens` to zero so
        # unrelated tests round-trip.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "analytics_agent_runs": [("reported", 3)],
        }
        rows = analytics_read.get_cost_coverage(connect=_connector(conn))
        self.assertEqual(rows[0].total_tokens, 0)


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


class ThroughputBreakdownTest(unittest.TestCase):
    """`get_throughput_breakdown` counts `stage_enter` rows whose
    stage is `done` (resolved) or `rejected`, grouped by day. It
    honors the standard event / stage filter contract by
    short-circuiting when the operator excludes the rows it would
    otherwise count."""

    def test_unset_db_url_returns_empty(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            analytics_read.get_throughput_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_event_filter_excluding_stage_enter_short_circuits(self) -> None:
        # If `stage_enter` is not in the events selection, this
        # widget has nothing to count -- it is by definition about
        # `stage_enter` rows.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_throughput_breakdown(
            events=["agent_exit"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_throughput_breakdown(
            events=[], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_stages_short_circuits(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_throughput_breakdown(
            stages=[], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_stage_filter_excludes_done_and_rejected(self) -> None:
        # The operator selected only non-terminal stages -- nothing
        # to count.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        rows = analytics_read.get_throughput_breakdown(
            stages=["implementing", "validating"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_returns_per_day_resolved_rejected(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "WHEN stage = 'done'": [
                (date(2026, 5, 25), 3, 1),
                (date(2026, 5, 26), 5, 0),
            ],
        }
        rows = analytics_read.get_throughput_breakdown(
            connect=_connector(conn),
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].day, date(2026, 5, 25))
        self.assertEqual(rows[0].resolved, 3)
        self.assertEqual(rows[0].rejected, 1)
        self.assertEqual(rows[1].resolved, 5)
        self.assertEqual(rows[1].rejected, 0)
        sql, params = conn.executed[0]
        # Implicit `event = 'stage_enter'` predicate plus the
        # stage IN ('done', 'rejected') intersection.
        self.assertIn("event = %s", sql)
        self.assertIn("stage_enter", params)
        self.assertIn("stage IN", sql)
        self.assertIn("done", params)
        self.assertIn("rejected", params)

    def test_stage_filter_intersects_with_resolved_rejected_pair(self) -> None:
        # User picked `done` only; SQL must narrow to `stage = 'done'`
        # (via stage IN ('done',)) inside the implicit event filter.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"WHEN stage = 'done'": [(date(2026, 5, 25), 1, 0)]}
        rows = analytics_read.get_throughput_breakdown(
            stages=["done", "implementing"],
            connect=_connector(conn),
        )
        self.assertEqual(len(rows), 1)
        _, params = conn.executed[0]
        # `implementing` is not in the resolved/rejected pair so it
        # is dropped from the IN clause -- only `done` lands.
        self.assertIn("done", params)
        self.assertNotIn("rejected", params)
        self.assertNotIn("implementing", params)


class AnalyticsConnectionScopeTest(unittest.TestCase):
    """`analytics_connection` is a context manager that:

    - yields `None` when ``ANALYTICS_DB_URL`` is unset (so the
      dashboard's "no data" path still works);
    - opens the connection lazily via the injected factory and reuses
      the same connection across subsequent `with` blocks on the
      same thread (the whole point of the rewrite -- the previous
      `_query` opened/closed per call);
    - closes-and-replaces the cached connection when an
      `OperationalError` / `InterfaceError` (wrapped or raw) escapes
      the `with` block, so the next caller opens a fresh socket;
    - exposes `close_thread_local_connection()` for explicit
      teardown.
    """

    def setUp(self) -> None:
        # Each test reloads the module under a hermetic env -- that
        # gives a fresh `_thread_local` -- but explicitly drop any
        # stale entry first so a prior test's failure cannot bleed
        # into this one.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        analytics_read.close_thread_local_connection()
        self.analytics_read = analytics_read

    def test_yields_none_when_db_url_unset(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        with analytics_read.analytics_connection() as conn:
            self.assertIsNone(conn)

    def test_reuses_connection_across_scopes_on_same_thread(self) -> None:
        analytics_read = self.analytics_read
        opens: list[str] = []
        conn_obj = _FakeConnection()

        def _factory(url: str) -> _FakeConnection:
            opens.append(url)
            return conn_obj

        with analytics_read.analytics_connection(connect=_factory) as c1:
            self.assertIs(c1, conn_obj)
        with analytics_read.analytics_connection(connect=_factory) as c2:
            self.assertIs(c2, conn_obj)
        self.assertEqual(len(opens), 1)
        # Persistent connection: the CM must NOT close on normal exit.
        self.assertEqual(conn_obj.close_called, 0)

    def test_close_thread_local_closes_cached_connection(self) -> None:
        analytics_read = self.analytics_read
        conn_obj = _FakeConnection()
        with analytics_read.analytics_connection(
            connect=_connector(conn_obj)
        ):
            pass
        analytics_read.close_thread_local_connection()
        self.assertEqual(conn_obj.close_called, 1)
        # Idempotent: a second teardown does not raise or re-close.
        analytics_read.close_thread_local_connection()
        self.assertEqual(conn_obj.close_called, 1)

    def test_broken_connection_invalidates_thread_local(self) -> None:
        analytics_read = self.analytics_read

        # Stand-in for psycopg.OperationalError -- the class-name
        # match in `_is_broken_connection_exc` lets the test simulate
        # the broken-socket path without psycopg installed. The
        # `__name__` must match verbatim (no leading underscore).
        class OperationalError(Exception):
            pass

        first = _FakeConnection()
        second = _FakeConnection()
        sequence = iter([first, second])

        def _factory(_url: str) -> _FakeConnection:
            return next(sequence)

        # First scope opens `first`, raises a broken-connection error
        # mid-scope; the CM must close-and-discard `first` so the
        # next scope opens `second`.
        with self.assertRaises(OperationalError):
            with analytics_read.analytics_connection(connect=_factory):
                raise OperationalError("server closed the connection")
        self.assertEqual(first.close_called, 1)

        with analytics_read.analytics_connection(connect=_factory) as c2:
            self.assertIs(c2, second)
        # `second` is not closed on normal exit (persistent).
        self.assertEqual(second.close_called, 0)

    def test_unrelated_error_does_not_invalidate(self) -> None:
        # A SQL syntax error or programmer mistake is NOT a torn-down
        # socket; the cached connection must survive so subsequent
        # reads on the same thread reuse it.
        analytics_read = self.analytics_read
        conn_obj = _FakeConnection()

        def _factory(_url: str) -> _FakeConnection:
            return conn_obj

        with self.assertRaises(ValueError):
            with analytics_read.analytics_connection(connect=_factory):
                raise ValueError("not a broken socket")
        self.assertEqual(conn_obj.close_called, 0)
        with analytics_read.analytics_connection(connect=_factory) as c2:
            self.assertIs(c2, conn_obj)

    def test_switching_db_url_replaces_cached_connection(self) -> None:
        # The thread-local must be keyed on the resolved URL: if a
        # later `with` block on the same thread asks for a different
        # `db_url=`, the stale socket has to close before a fresh
        # one opens. Otherwise a thread that first read from DB A
        # would silently keep reading from A even after the caller
        # switched to DB B.
        analytics_read = self.analytics_read
        seen: list[str] = []
        first = _FakeConnection()
        second = _FakeConnection()
        sequence = iter([first, second])

        def _factory(url: str) -> _FakeConnection:
            seen.append(url)
            return next(sequence)

        with analytics_read.analytics_connection(
            db_url="postgresql://A/db", connect=_factory,
        ) as c1:
            self.assertIs(c1, first)
        with analytics_read.analytics_connection(
            db_url="postgresql://B/db", connect=_factory,
        ) as c2:
            self.assertIs(c2, second)
        self.assertEqual(seen, ["postgresql://A/db", "postgresql://B/db"])
        # `first` (opened for DB A) was closed when the URL changed.
        self.assertEqual(first.close_called, 1)
        # `second` persists for further reads on DB B.
        self.assertEqual(second.close_called, 0)

    def test_same_url_does_not_reopen(self) -> None:
        # Sanity: re-entering with the same explicit URL on the same
        # thread reuses the cached connection -- the URL-change
        # invalidation must not over-trigger.
        analytics_read = self.analytics_read
        opens: list[str] = []
        cached = _FakeConnection()

        def _factory(url: str) -> _FakeConnection:
            opens.append(url)
            return cached

        with analytics_read.analytics_connection(
            db_url="postgresql://h/db", connect=_factory,
        ):
            pass
        with analytics_read.analytics_connection(
            db_url="postgresql://h/db", connect=_factory,
        ) as c2:
            self.assertIs(c2, cached)
        self.assertEqual(opens, ["postgresql://h/db"])

    def test_persistent_factory_sets_autocommit(self) -> None:
        # The default factory wraps `psycopg.connect(db_url,
        # autocommit=True)` so a long-lived thread-local socket does
        # not leave the session idle in transaction after every
        # SELECT. We stub `psycopg.connect` to capture the kwargs;
        # the real driver does not need to be installed for this
        # test to validate the contract.
        analytics_read = self.analytics_read
        captured: dict[str, Any] = {}

        class _FakePsycopg:
            class OperationalError(Exception):
                pass

            class InterfaceError(Exception):
                pass

            @staticmethod
            def connect(url: str, **kwargs: Any) -> _FakeConnection:
                captured["url"] = url
                captured["kwargs"] = kwargs
                return _FakeConnection()

        with patch.dict(sys.modules, {"psycopg": _FakePsycopg}):
            conn = analytics_read._default_persistent_connect(
                "postgresql://h/db"
            )
        self.assertEqual(captured["url"], "postgresql://h/db")
        self.assertEqual(captured["kwargs"].get("autocommit"), True)
        self.assertIsNotNone(conn)


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


class IsBrokenConnectionExcTest(unittest.TestCase):
    """The broken-connection detector unwraps `AnalyticsReadError`
    (every driver-level failure goes through `_query` which wraps)
    and matches by class name so a fake without psycopg installed
    can drive the close-and-replace path.
    """

    def test_matches_by_class_name(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})

        class OperationalError(Exception):
            pass

        class InterfaceError(Exception):
            pass

        self.assertTrue(
            analytics_read._is_broken_connection_exc(
                OperationalError("dead")
            )
        )
        self.assertTrue(
            analytics_read._is_broken_connection_exc(
                InterfaceError("dead")
            )
        )

    def test_unwraps_analytics_read_error(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})

        class OperationalError(Exception):
            pass

        wrapper = analytics_read.AnalyticsReadError("wrap")
        wrapper.__cause__ = OperationalError("dead")
        self.assertTrue(
            analytics_read._is_broken_connection_exc(wrapper)
        )

    def test_unrelated_error_is_not_broken(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        self.assertFalse(
            analytics_read._is_broken_connection_exc(
                ValueError("not a broken socket")
            )
        )


class RollupReadCutoverTest(unittest.TestCase):
    """Layer 4 cutover: every reader the issue calls out reads from
    `analytics_daily_rollup` instead of `analytics_events` /
    `analytics_agent_runs`. The previous reader-shape tests above
    already cover the column wiring; this class concentrates on the
    semantic invariants the cutover has to preserve.

    The rollup is keyed on `(day, repo, issue, event, stage,
    backend, cost_source)` with `day = (ts AT TIME ZONE 'UTC')::date`
    and aggregates `event_count`, `failed_count`, `timed_out_count`,
    `total_cost_usd`, the token sums, and `duration_s_sum` /
    `duration_s_count`. The dashboard passes midnight-aligned UTC
    `[start, end)` windows so the rollup is semantically equivalent
    to a `ts`-scoped scan; these tests pin that down by checking
    parameter bindings, filter shapes, and column accounting.
    """

    def _rollup_readers(self, analytics_read) -> list[Callable[..., Any]]:
        # The seven cutover readers in the order the issue lists
        # them. `get_summary` and `get_kpi_prev` carry the same
        # `_build_rollup_window_where` shape; `get_throughput_breakdown`
        # builds its WHERE inline but still uses `day` rather than `ts`.
        return [
            analytics_read.get_summary,
            analytics_read.get_kpi_prev,
            analytics_read.get_time_series,
            analytics_read.get_stage_breakdown,
            analytics_read.get_repo_breakdown,
            analytics_read.get_backend_efficiency,
            analytics_read.get_throughput_breakdown,
        ]

    def test_every_cutover_reader_queries_the_rollup(self) -> None:
        # No cutover reader may regress to `analytics_events` or
        # `analytics_agent_runs` -- the whole point of the migration
        # is the rollup-backed scan. A single check against every
        # reader in one place keeps a future reader rewrite from
        # silently dropping the rollup target.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        for reader in self._rollup_readers(analytics_read):
            conn = _FakeConnection()
            reader(connect=_connector(conn))
            self.assertEqual(
                len(conn.executed), 1,
                f"{reader.__name__} must issue one round-trip",
            )
            sql = conn.executed[0][0]
            self.assertIn(
                "FROM analytics_daily_rollup", sql,
                f"{reader.__name__} must read from the rollup, "
                f"got SQL: {sql}",
            )
            self.assertNotIn(
                "FROM analytics_events", sql,
                f"{reader.__name__} must not regress to "
                f"analytics_events",
            )
            self.assertNotIn(
                "FROM analytics_agent_runs", sql,
                f"{reader.__name__} must not regress to "
                f"analytics_agent_runs",
            )

    def test_window_predicate_uses_day_with_date_params(self) -> None:
        # The dashboard's `to_window` produces midnight-aligned UTC
        # datetimes; the rollup is keyed by `day` (a UTC date), so
        # the helper must project `start`/`end` to `.date()` before
        # binding so the query plan stays a day-range scan rather
        # than a cast at execute time.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 28, tzinfo=timezone.utc)
        for reader in self._rollup_readers(analytics_read):
            conn = _FakeConnection()
            reader(start=start, end=end, connect=_connector(conn))
            sql, params = conn.executed[0]
            self.assertIn(
                "day >= %s", sql,
                f"{reader.__name__} must use day-keyed lower bound",
            )
            self.assertIn(
                "day < %s", sql,
                f"{reader.__name__} must use day-keyed upper bound",
            )
            self.assertIn(start.date(), params)
            self.assertIn(end.date(), params)

    def test_issue_filter_narrows_every_reader(self) -> None:
        # The rollup key carries `issue`, so the `issue = %s`
        # predicate still narrows the scan. The dashboard refuses
        # to apply this filter unless `repo` is also set (issue
        # numbers are only unique within a repo); the helper itself
        # does not enforce that, so we mirror the dashboard's call
        # shape (`repo=..., issue=...`).
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        for reader in self._rollup_readers(analytics_read):
            conn = _FakeConnection()
            reader(repo="owner/r", issue=42, connect=_connector(conn))
            sql, params = conn.executed[0]
            self.assertIn(
                "issue = %s", sql,
                f"{reader.__name__} must thread the issue filter "
                f"into the rollup scan",
            )
            self.assertIn(42, params)

    def test_event_filter_clears_to_empty_predicate(self) -> None:
        # Cleared-multiselect contract: an empty list means "no
        # rows match" rather than "no filter". `get_backend_efficiency`
        # is excluded because it short-circuits via
        # `_agent_event_excluded` before building SQL (cleared events
        # selection = no agent_exit selected = return []); the other
        # cutover readers that take an `events=` param emit the
        # tautologically-false predicate. `get_throughput_breakdown`
        # has its own short-circuit on the implicit `stage_enter`
        # constraint -- so it also returns [] without SQL when
        # events is cleared.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        emits_predicate = [
            analytics_read.get_summary,
            analytics_read.get_kpi_prev,
            analytics_read.get_time_series,
            analytics_read.get_stage_breakdown,
            analytics_read.get_repo_breakdown,
        ]
        for reader in emits_predicate:
            conn = _FakeConnection()
            reader(events=[], connect=_connector(conn))
            sql = conn.executed[0][0]
            self.assertIn(
                "FALSE", sql,
                f"{reader.__name__} must emit a tautologically-false "
                f"predicate when the events multiselect is cleared",
            )

    def test_stage_filter_clears_to_empty_predicate(self) -> None:
        # Mirrors the events-filter contract: an empty stages list
        # is the dashboard's cleared-multiselect signal. Same set
        # of readers as `test_event_filter_clears_to_empty_predicate`
        # because `get_backend_efficiency` does not short-circuit on
        # stages, but the FALSE predicate is what makes its result
        # drop to zero alongside the rest.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        emits_predicate = [
            analytics_read.get_summary,
            analytics_read.get_kpi_prev,
            analytics_read.get_time_series,
            analytics_read.get_stage_breakdown,
            analytics_read.get_repo_breakdown,
            analytics_read.get_backend_efficiency,
        ]
        for reader in emits_predicate:
            conn = _FakeConnection()
            reader(stages=[], connect=_connector(conn))
            sql = conn.executed[0][0]
            self.assertIn(
                "FALSE", sql,
                f"{reader.__name__} must emit a tautologically-false "
                f"predicate when the stages multiselect is cleared",
            )

    def test_summary_recovers_token_and_timeout_aggregates(self) -> None:
        # The dashboard's KPI strip reads `total_input_tokens`,
        # `total_output_tokens`, `total_cache_read_tokens`,
        # `total_cache_write_tokens`, `total_cost_usd`, and
        # `timed_out_agent_runs` off `get_summary`. The rollup
        # carries the per-bucket sums under `total_*` columns and
        # `timed_out_count` is pre-scoped to `event = 'agent_exit'
        # AND timed_out = TRUE`, so a plain SUM recovers each
        # KPI's value verbatim. This test pins the column
        # accounting end-to-end so a future rollup column rename
        # cannot silently zero out a KPI.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # 13-column totals row matching the cutover SQL's projection
        # order: kind / label / events / issues / repos / cost /
        # input / output / total_runs / failed_runs / cache_read /
        # cache_write / timed_out.
        conn.rows_for = {
            "WITH win AS": [
                ("t", None, 200, 24, 3,
                 4.5, 12_000, 8_000, 35, 6, 3_000, 1_500, 11),
            ],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.total_input_tokens, 12_000)
        self.assertEqual(result.total_output_tokens, 8_000)
        self.assertEqual(result.total_cache_read_tokens, 3_000)
        self.assertEqual(result.total_cache_write_tokens, 1_500)
        self.assertEqual(result.total_cost_usd, 4.5)
        self.assertEqual(result.total_agent_runs, 35)
        self.assertEqual(result.failed_agent_runs, 6)
        # The reliability "Timeouts" tile reads off this field --
        # the rollup's `timed_out_count` is already
        # `event = 'agent_exit' AND timed_out = TRUE`-scoped, so a
        # plain SUM is correct without an extra CASE in the reader.
        self.assertEqual(result.timed_out_agent_runs, 11)
        sql, _ = conn.executed[0]
        self.assertIn("SUM(timed_out_count)", sql)
        self.assertIn("SUM(total_input_tokens)", sql)
        self.assertIn("SUM(total_output_tokens)", sql)
        self.assertIn("SUM(total_cache_read_tokens)", sql)
        self.assertIn("SUM(total_cache_write_tokens)", sql)

    def test_stage_breakdown_recovers_weighted_duration_average(self) -> None:
        # `AVG(duration_s)` cannot be reconstructed from per-day
        # rollup averages without double-averaging (averaging
        # averages across days does not preserve the row-weighted
        # mean), so the rollup carries `duration_s_sum` and
        # `duration_s_count` separately and the reader recovers
        # `AVG` as `SUM(sum) / SUM(count)`. The fake's pre-computed
        # `avg_dur` here mirrors what the SQL division produces;
        # the test pins the SQL shape so a future regression to a
        # naive `AVG(duration_s)` would fail.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        # (stage, count, avg_dur, cost, input_tok, output_tok, runs)
        # Two stages: implementing (sum=125, count=10 -> 12.5),
        # validating (no row carried a non-null duration -> NULL).
        conn.rows_for = {
            "FROM analytics_daily_rollup": [
                ("implementing", 10, 12.5, 0.50, 0, 0, 10),
                ("validating", 3, None, 0.05, 0, 0, 3),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].stage, "implementing")
        self.assertEqual(rows[0].avg_duration_s, 12.5)
        # NULL preserved when no row in the window carried a
        # duration -- the dashboard hides the column rather than
        # showing a misleading zero.
        self.assertIsNone(rows[1].avg_duration_s)
        sql, _ = conn.executed[0]
        self.assertIn("SUM(duration_s_sum)", sql)
        self.assertIn("NULLIF(SUM(duration_s_count), 0)", sql)
        # Regression guard: the cutover MUST NOT regress to a plain
        # `AVG(duration_s)` over the rollup -- the rollup has no
        # such column, but more importantly averaging averages
        # across days would silently produce wrong numbers.
        self.assertNotIn("AVG(duration_s)", sql)

    def test_backend_efficiency_pins_event_filter_in_sql(self) -> None:
        # The previous `analytics_agent_runs` view filtered to
        # `event = 'agent_exit'` internally. The rollup has an
        # `event` column, so the reader pins the filter in the
        # WHERE clause directly -- this is how the cutover
        # preserves the prior view's row scope.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_backend_efficiency(connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("event = 'agent_exit'", sql)
        # And the agent-event short-circuit still wins over the
        # pinned filter when the operator excludes `agent_exit`
        # from the multiselect: no SQL emitted.
        conn = _FakeConnection()
        rows = analytics_read.get_backend_efficiency(
            events=["stage_enter"], connect=_connector(conn),
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_throughput_breakdown_uses_day_window(self) -> None:
        # `get_throughput_breakdown` builds its WHERE clause inline
        # (it carries a hardcoded `event = 'stage_enter'` predicate),
        # so the Layer 4 cutover has to migrate that branch too.
        # The window must bind `.date()` values against the rollup
        # `day` column rather than the previous `ts >= / ts <`
        # pair against the events table.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 28, tzinfo=timezone.utc)
        analytics_read.get_throughput_breakdown(
            start=start, end=end, repo="owner/r", issue=42,
            connect=_connector(conn),
        )
        sql, params = conn.executed[0]
        self.assertIn("FROM analytics_daily_rollup", sql)
        self.assertIn("day >= %s", sql)
        self.assertIn("day < %s", sql)
        self.assertIn("event = %s", sql)
        self.assertIn("stage_enter", params)
        # `.date()` binding so the planner sees a date-range scan
        # against the `(day, repo)` supporting index.
        self.assertIn(start.date(), params)
        self.assertIn(end.date(), params)
        self.assertIn("owner/r", params)
        self.assertIn(42, params)


class RawReaderRollupKeepsTest(unittest.TestCase):
    """The issue is explicit about which readers stay on the raw
    table or the agent-run view: recent agent exits, top-cost
    issues, review-round breakdown, hourly heatmap, issue events,
    and cost coverage. The other view-backed read
    (`get_backend_daily_tokens`) and `get_event_breakdown` also stay
    where they are. This test class is a regression guard so a
    future change cannot quietly move one of them to the rollup
    where it would lose row-level detail (`ts` precision,
    `review_round`, `retry_count`, hour-of-day).
    """

    def test_recent_agent_exits_reads_base_table(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_recent_agent_exits(connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_events", sql)
        self.assertNotIn("FROM analytics_daily_rollup", sql)

    def test_top_cost_issues_reads_base_table(self) -> None:
        # `get_issues` carries MIN(ts), MAX(ts), `latest_stage`,
        # MAX(review_round), and MAX(retry_count) which the rollup
        # cannot answer -- the rollup throws away the per-row `ts`
        # precision and never carried `review_round` / `retry_count`.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_issues(connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_events", sql)
        self.assertNotIn("FROM analytics_daily_rollup", sql)

    def test_review_round_breakdown_stays_on_view(self) -> None:
        # `review_round` is not in the rollup key, so the rollup
        # cannot bucket by it. Stays on `analytics_agent_runs`.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_review_round_breakdown(connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_agent_runs", sql)
        self.assertNotIn("FROM analytics_daily_rollup", sql)

    def test_hourly_heatmap_stays_on_base_table(self) -> None:
        # The rollup is day-bucketed -- hour-of-day is not
        # recoverable from `day`, so this widget must keep reading
        # from `analytics_events`.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_hourly_heatmap(connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_events", sql)
        self.assertNotIn("FROM analytics_daily_rollup", sql)

    def test_issue_events_stays_on_base_table(self) -> None:
        # Per-row drill-down -- the rollup pre-aggregates per group
        # so individual rows are no longer addressable.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_issue_events(
            repo="owner/r", issue=1, connect=_connector(conn),
        )
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_events", sql)
        self.assertNotIn("FROM analytics_daily_rollup", sql)

    def test_cost_coverage_stays_on_view(self) -> None:
        # Cost coverage stays on `analytics_agent_runs` per the
        # issue's "unless the rollup can match behavior exactly"
        # guardrail -- being conservative here lets the
        # `unknown-price` cohort's run / token accounting stay
        # exactly as it was.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_cost_coverage(connect=_connector(conn))
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_agent_runs", sql)
        self.assertNotIn("FROM analytics_daily_rollup", sql)


if __name__ == "__main__":
    unittest.main()
