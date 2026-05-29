# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone
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
    """Reload `orchestrator.config` and `orchestrator.analytics_read`
    against the given hermetic env, mirroring `test_analytics_sync`.

    Import order matters: `config` must come first so its fresh module
    object is installed as the `orchestrator.config` package attribute
    before `analytics_read`'s `from . import config` runs. Otherwise
    Python's `_handle_fromlist` shortcut returns the stale attribute
    that conftest's eager `from orchestrator import config` left
    behind, and `analytics_read.config` keeps reading the
    pre-`patch.dict` env values.
    """
    with patch.dict(os.environ, _hermetic_env(env), clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.analytics_read", None)
        import orchestrator.config as config
        import orchestrator.analytics_read as analytics_read
        return config, analytics_read


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
        conn.rows_for = {
            "DISTINCT repo": [("owner/a",), ("owner/b",)],
            "DISTINCT event": [("agent_exit",), ("stage_enter",)],
            "DISTINCT stage": [("implementing",), ("validating",)],
            "DISTINCT backend": [("claude",), ("codex",)],
            "DISTINCT agent_role": [("dev",), ("review",)],
        }
        result = analytics_read.get_filter_options(connect=_connector(conn))
        self.assertEqual(result.repos, ("owner/a", "owner/b"))
        self.assertEqual(result.events, ("agent_exit", "stage_enter"))
        self.assertEqual(result.stages, ("implementing", "validating"))
        self.assertEqual(result.backends, ("claude", "codex"))
        self.assertEqual(result.agent_roles, ("dev", "review"))
        # One SELECT DISTINCT per column.
        self.assertEqual(len(conn.executed), 5)
        # Each SELECT excludes NULLs via the WHERE clause.
        for sql, _ in conn.executed:
            self.assertIn("IS NOT NULL", sql)
        # Connection is closed once per query (each query opens a
        # fresh `connect_fn(db_url)`); with the same fake returned
        # every time, 5 close calls means each `_query` call cleaned
        # up after itself.
        self.assertEqual(conn.close_called, 5)

    def test_drops_null_rows(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "DISTINCT repo": [("owner/a",), (None,), ("owner/b",)],
            "DISTINCT event": [],
            "DISTINCT stage": [],
            "DISTINCT backend": [],
            "DISTINCT agent_role": [],
        }
        result = analytics_read.get_filter_options(connect=_connector(conn))
        self.assertEqual(result.repos, ("owner/a", "owner/b"))


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
        # No rows from any of the three SELECTs.
        conn.rows_for = {}
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result, analytics_read.Summary())

    def test_aggregates_and_breakdowns(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "COUNT(*) AS total_events": [(42, 10, 2, 1.234, 100, 200)],
            "GROUP BY event": [("stage_enter", 30), ("agent_exit", 12)],
            "GROUP BY stage": [("implementing", 20), ("validating", 10)],
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
        self.assertEqual(result.by_event, {"stage_enter": 30, "agent_exit": 12})
        self.assertEqual(result.by_stage, {"implementing": 20, "validating": 10})

    def test_window_and_repo_params_bound(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        end = datetime(2026, 5, 28, tzinfo=timezone.utc)
        analytics_read.get_summary(
            start=start, end=end, repo="owner/r",
            connect=_connector(conn),
        )
        # Every executed SQL carries the same window params for the
        # totals + breakdown queries.
        for sql, params in conn.executed:
            self.assertIn("ts >= %s", sql)
            self.assertIn("ts < %s", sql)
            self.assertIn("repo = %s", sql)
            self.assertEqual(params[:3], (start, end, "owner/r"))

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
            "COUNT(*) AS total_events": [(4, 2, 2, 0.0, 0, 0)],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.distinct_issues, 2)
        totals_sql, _ = conn.executed[0]
        self.assertIn("COUNT(DISTINCT (repo, issue))", totals_sql)


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
        conn.rows_for = {
            "date_trunc('day', ts)": [
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
        # Some drivers return `date_trunc(...)` as a timestamp; the
        # read model normalises so the dashboard sees `date`.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "date_trunc('day', ts)": [
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
        conn.rows_for = {
            "AVG(duration_s)": [
                ("implementing", 20, 12.5),
                ("validating", 10, None),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].stage, "implementing")
        self.assertEqual(rows[0].count, 20)
        self.assertEqual(rows[0].avg_duration_s, 12.5)
        self.assertIsNone(rows[1].avg_duration_s)
        # `IS NOT NULL` guard on stage is present.
        self.assertIn("stage IS NOT NULL", conn.executed[0][0])

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
        # came back.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"DISTINCT repo": [("owner/a",)]}

        def _bad_close():
            raise RuntimeError("close failed")

        conn.close = _bad_close  # type: ignore[method-assign]
        # Drive only the first distinct query path; the others would
        # also try to close and raise, masking the swallow semantics.
        result = analytics_read._distinct_strings(
            _connector(conn), "postgresql://h/db", "repo",
        )
        self.assertEqual(result, ("owner/a",))


class DefaultDbUrlTest(unittest.TestCase):
    """When no `db_url` kwarg is passed, `config.ANALYTICS_DB_URL`
    is the default."""

    def test_config_url_used_when_kwarg_omitted(self) -> None:
        config, analytics_read = _reload(
            {"ANALYTICS_DB_URL": "postgresql://from-env/db"}
        )
        seen: list[str] = []

        def _capture_connect(url: str) -> _FakeConnection:
            seen.append(url)
            return _FakeConnection()

        analytics_read.get_filter_options(connect=_capture_connect)
        self.assertEqual(seen[0], "postgresql://from-env/db")
        self.assertEqual(config.ANALYTICS_DB_URL, "postgresql://from-env/db")

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


if __name__ == "__main__":
    unittest.main()
