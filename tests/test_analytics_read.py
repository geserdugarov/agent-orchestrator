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
        totals_sql, totals_params = conn.executed[0]
        self.assertIn("event IN (%s, %s)", totals_sql)
        self.assertIn("stage IN (%s)", totals_sql)
        self.assertIn("agent_exit", totals_params)
        self.assertIn("stage_enter", totals_params)
        self.assertIn("implementing", totals_params)

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
        # Extended 8-tuple: events / issues / repos / cost / in / out
        # / total runs / failed runs.
        conn.rows_for = {
            "COUNT(*) AS total_events": [(50, 12, 3, 2.5, 100, 200, 15, 4)],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.total_agent_runs, 15)
        self.assertEqual(result.failed_agent_runs, 4)
        totals_sql, _ = conn.executed[0]
        self.assertIn("total_agent_runs", totals_sql)
        self.assertIn("failed_agent_runs", totals_sql)
        # Failure subset constrains on event='agent_exit' AND
        # exit_code <> 0 so NULL exit codes never count as failures.
        self.assertIn("exit_code <> 0", totals_sql)

    def test_legacy_6tuple_fixture_round_trips(self) -> None:
        # A test that wasn't taught about the extension still
        # returns zeros for the new fields instead of unpack errors.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "COUNT(*) AS total_events": [(4, 2, 2, 0.0, 0, 0)],
        }
        result = analytics_read.get_summary(connect=_connector(conn))
        self.assertEqual(result.total_agent_runs, 0)
        self.assertEqual(result.failed_agent_runs, 0)


class TimeSeriesAggregatesTest(unittest.TestCase):
    """Reshaped `get_time_series` carries per-(day, event) cost /
    token aggregates so the spend-over-time and tokens-over-time
    charts can pivot the same query."""

    def test_aggregates_round_trip(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "date_trunc('day', ts)": [
                (date(2026, 5, 25), "agent_exit", 3, 0.42, 1000, 500),
            ],
        }
        points = analytics_read.get_time_series(connect=_connector(conn))
        self.assertEqual(len(points), 1)
        p = points[0]
        self.assertEqual(p.count, 3)
        self.assertEqual(p.cost_usd, 0.42)
        self.assertEqual(p.input_tokens, 1000)
        self.assertEqual(p.output_tokens, 500)
        sql, _ = conn.executed[0]
        self.assertIn("SUM(cost_usd)", sql)
        self.assertIn("SUM(input_tokens)", sql)
        self.assertIn("SUM(output_tokens)", sql)


class StageBreakdownExtensionTest(unittest.TestCase):
    """Extended `get_stage_breakdown` rolls up cost and token totals
    per stage so the breakdown table can show "spend per stage"."""

    def test_rolls_up_cost_and_tokens(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "AVG(duration_s)": [
                ("implementing", 20, 12.5, 0.50, 2000, 1500),
                ("validating", 10, None, 0.10, 100, 200),
            ],
        }
        rows = analytics_read.get_stage_breakdown(connect=_connector(conn))
        self.assertEqual(rows[0].total_cost_usd, 0.50)
        self.assertEqual(rows[0].total_input_tokens, 2000)
        self.assertEqual(rows[0].total_output_tokens, 1500)
        self.assertEqual(rows[1].total_cost_usd, 0.10)
        sql, _ = conn.executed[0]
        self.assertIn("SUM(cost_usd)", sql)


class IssuesExtensionTest(unittest.TestCase):
    """Extended `get_issues` adds the highest review round any agent
    run for the issue reached and how many of those runs exited
    non-zero. Both are zero-defaulted so old 10-tuple fixtures still
    round-trip."""

    def test_extended_columns_round_trip(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        t = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
        conn.rows_for = {
            "GROUP BY repo, issue": [
                ("owner/r", 7, 8, t, t, "implementing", 5,
                 0.55, 800, 400, 3, 2),
            ],
        }
        rows = analytics_read.get_issues(connect=_connector(conn))
        self.assertEqual(rows[0].max_review_round, 3)
        self.assertEqual(rows[0].failed_agent_runs, 2)
        sql, _ = conn.executed[0]
        self.assertIn("MAX(review_round)", sql)
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
        conn.rows_for = {
            "analytics_agent_runs": [
                ("0", 12, 1),
                ("1", 8, 2),
                ("3-5", 4, 4),
                ("unknown", 1, 0),
            ],
        }
        rows = analytics_read.get_review_round_breakdown(
            connect=_connector(conn),
        )
        self.assertEqual([r.bucket for r in rows], ["0", "1", "3-5", "unknown"])
        self.assertEqual([r.runs for r in rows], [12, 8, 4, 1])
        self.assertEqual([r.failed for r in rows], [1, 2, 4, 0])
        sql, _ = conn.executed[0]
        # Reads from the view, not the base table, and the view has
        # no `event` column so no `event IN (...)` clause is emitted.
        self.assertIn("FROM analytics_agent_runs", sql)
        self.assertNotIn("event IN", sql)

    def test_explicit_agent_exit_runs_query(self) -> None:
        # An events list that includes agent_exit must NOT short-circuit
        # -- the operator still wants to see the agent runs view.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {"analytics_agent_runs": [("1", 3, 0)]}
        rows = analytics_read.get_review_round_breakdown(
            events=["agent_exit", "stage_enter"],
            connect=_connector(conn),
        )
        self.assertEqual(len(rows), 1)


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
        conn.rows_for = {
            "analytics_agent_runs": [
                ("claude", 20, 1, 35.0, 1.20, 5000, 4000),
                ("codex", 10, 3, None, 0.40, 1000, 2000),
                ("unknown", 1, 0, None, 0.0, 0, 0),
            ],
        }
        rows = analytics_read.get_backend_efficiency(connect=_connector(conn))
        self.assertEqual([r.backend for r in rows], ["claude", "codex", "unknown"])
        self.assertEqual(rows[0].runs, 20)
        self.assertEqual(rows[0].failed, 1)
        self.assertEqual(rows[0].avg_duration_s, 35.0)
        self.assertEqual(rows[0].total_cost_usd, 1.20)
        # NULL avg duration preserved so the dashboard can hide the
        # column rather than show a misleading zero.
        self.assertIsNone(rows[1].avg_duration_s)
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_agent_runs", sql)
        self.assertIn("COALESCE(backend, 'unknown')", sql)


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
        # because rows are already scoped to one repo per bucket.
        self.assertIn("COUNT(DISTINCT issue)", sql)
        self.assertIn("FROM analytics_events", sql)

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
        # which runs the parser could not price.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        conn.rows_for = {
            "analytics_agent_runs": [
                ("reported", 20),
                ("estimated", 5),
                ("unknown-price", 3),
                ("no-usage", 2),
                ("unknown", 1),
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
        sql, _ = conn.executed[0]
        self.assertIn("FROM analytics_agent_runs", sql)
        # NULL cost_source rows bucket under "unknown" via COALESCE,
        # but the verbatim "unknown-price" string is untouched.
        self.assertIn("COALESCE(cost_source, 'unknown')", sql)


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
        conn.rows_for = {
            "EXTRACT(DOW FROM ts)": [
                (1, 9, 5),  # Monday 09:00 -- 5 events
                (1, 14, 7),
                (3, 22, 2),
            ],
        }
        cells = analytics_read.get_hourly_heatmap(connect=_connector(conn))
        self.assertEqual(len(cells), 3)
        self.assertEqual((cells[0].weekday, cells[0].hour, cells[0].count),
                         (1, 9, 5))
        sql, _ = conn.executed[0]
        self.assertIn("EXTRACT(DOW FROM ts)", sql)
        self.assertIn("EXTRACT(HOUR FROM ts)", sql)
        self.assertIn("FROM analytics_events", sql)

    def test_event_filter_threaded(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        conn = _FakeConnection()
        analytics_read.get_hourly_heatmap(
            events=["agent_exit"], connect=_connector(conn),
        )
        sql, params = conn.executed[0]
        self.assertIn("event IN (%s)", sql)
        self.assertIn("agent_exit", params)


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


if __name__ == "__main__":
    unittest.main()
