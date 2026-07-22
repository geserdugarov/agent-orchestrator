# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics issue-summary read tests."""

import unittest


from datetime import datetime, timezone


from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)


from tests.analytics_assertions import (
    assert_row_fields,
)


_WINDOW_END_DAY = 28


_EVENT_TS_DAY = 25


_NOON_TS_DAY = 25


_NOON_TS_HOUR = 12


_LATER_SEEN_DAY = 26


_LATEST_SEEN_DAY = 26


_LATEST_SEEN_MINUTE = 30


_REPO_ISSUE_PAIR_TOTAL_COST_USD = 0.42


_REPO_ISSUE_PAIR_TOTAL_INPUT_TOKENS = 500


_REPO_ISSUE_PAIR_TOTAL_OUTPUT_TOKENS = 300


_REPO_PARAMS_BOUND_LIMIT = 25


_GROUP_BY_PAIR = "GROUP BY repo, issue"


_STAGE_IMPLEMENTING = "implementing"


_STAGE_VALIDATING = "validating"


_REPO_SHORT = "owner/r"


_YEAR = 2026


_WINDOW_START = datetime(_YEAR, 5, 1, tzinfo=timezone.utc)


_WINDOW_END = datetime(_YEAR, 5, _WINDOW_END_DAY, tzinfo=timezone.utc)


_EVENT_TS = datetime(_YEAR, 5, _EVENT_TS_DAY, 10, 0, tzinfo=timezone.utc)


_NOON_TS = datetime(_YEAR, 5, _NOON_TS_DAY, _NOON_TS_HOUR, 0, tzinfo=timezone.utc)


_LATER_SEEN = datetime(_YEAR, 5, _LATER_SEEN_DAY, 9, 0, tzinfo=timezone.utc)


_LATEST_SEEN = datetime(_YEAR, 5, _LATEST_SEEN_DAY, 9, _LATEST_SEEN_MINUTE, tzinfo=timezone.utc)


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
                    "owner/b",
                    1,
                    3,
                    _LATER_SEEN,
                    _LATEST_SEEN,
                    _STAGE_VALIDATING,
                    1,
                    0.42,
                    500,
                    300,
                ),
                (
                    "owner/a",
                    1,
                    5,
                    _EVENT_TS,
                    _NOON_TS,
                    _STAGE_IMPLEMENTING,
                    2,
                    None,
                    0,
                    0,
                ),
            ],
        }
        issues = analytics_read.get_issues(connect=conn.as_connect)
        # Order preserved from the SQL; the second row's `None` cost
        # survives as `None` rather than coercing to 0.0.
        self.assertEqual(len(issues), 2)
        self.assertEqual(
            (issues[0].repo, issues[0].issue),
            ("owner/b", 1),
        )
        self.assertEqual(
            (issues[1].repo, issues[1].issue),
            ("owner/a", 1),
        )
        assert_row_fields(
            self,
            issues[0],
            {
                "event_count": 3,
                "first_seen": _LATER_SEEN,
                "last_seen": _LATEST_SEEN,
                "latest_stage": _STAGE_VALIDATING,
                "agent_exits": 1,
                "total_cost_usd": _REPO_ISSUE_PAIR_TOTAL_COST_USD,
                "total_input_tokens": _REPO_ISSUE_PAIR_TOTAL_INPUT_TOKENS,
                "total_output_tokens": _REPO_ISSUE_PAIR_TOTAL_OUTPUT_TOKENS,
            },
        )
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
            start=_WINDOW_START,
            end=_WINDOW_END,
            repo=_REPO_SHORT,
            limit=_REPO_PARAMS_BOUND_LIMIT,
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
                (_REPO_SHORT, 7, 8, _EVENT_TS, _EVENT_TS, _STAGE_IMPLEMENTING, 5, 0.55, 800, 400, 3, 2, 4),
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
