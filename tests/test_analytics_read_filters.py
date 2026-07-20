# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

# Event / stage names and the repo slug the dashboard threads into
# every read; each recurs across the module's filter assertions.
_AGENT_EXIT = "agent_exit"
_STAGE_ENTER = "stage_enter"
_STAGE_IMPLEMENTING = "implementing"
_REPO_SHORT = "owner/r"

# Shared window bounds. `get_recent_agent_exits` / `get_issue_events`
# scan the raw events table, so the `ts` predicates bind these
# datetimes directly rather than a `.date()` projection.
_YEAR = 2026
_WINDOW_START = datetime(_YEAR, 5, 1, tzinfo=timezone.utc)
_WINDOW_END = datetime(_YEAR, 5, 28, tzinfo=timezone.utc)


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
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_summary(
            events=[_AGENT_EXIT, _STAGE_ENTER],
            stages=[_STAGE_IMPLEMENTING],
            connect=conn.as_connect,
        )
        # Layer 3's combined SQL applies the filter once in the CTE;
        # the totals + breakdown branches inherit from it.
        self.assertEqual(len(conn.executed), 1)
        sql, query_params = conn.first_query
        self.assertIn("event IN (%s, %s)", sql)
        self.assertIn("stage IN (%s)", sql)
        self.assertIn(_AGENT_EXIT, query_params)
        self.assertIn(_STAGE_ENTER, query_params)
        self.assertIn(_STAGE_IMPLEMENTING, query_params)

    def test_empty_events_emits_false_predicate(self) -> None:
        # The dashboard's "cleared multiselect" case: an empty list
        # means "no rows match" rather than "no filter". The SQL
        # carries a tautologically-false predicate; the database
        # never returns any row.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_summary(events=[], connect=conn.as_connect)
        for sql, _ in conn.executed:
            self.assertIn("FALSE", sql)

    def test_empty_stages_emits_false_predicate(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_time_series(stages=[], connect=conn.as_connect)
        sql, _ = conn.first_query
        self.assertIn("FALSE", sql)

    def test_issue_filter_narrows_summary(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_summary(
            repo=_REPO_SHORT, issue=42, connect=conn.as_connect,
        )
        sql, query_params = conn.first_query
        self.assertIn("issue = %s", sql)
        self.assertIn(42, query_params)


class RecentAgentExitsFilterTest(unittest.TestCase):
    """The reviewer flagged that `get_recent_agent_exits` ignored
    the sidebar date window. The function now accepts `start`,
    `end`, `events`, `stages`, and `issue` so the recent-runs table
    narrows with the rest of the dashboard.
    """

    def test_date_window_threaded_into_where(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_recent_agent_exits(
            limit=10, start=_WINDOW_START, end=_WINDOW_END, repo=_REPO_SHORT,
            connect=conn.as_connect,
        )
        sql, query_params = conn.first_query
        self.assertIn("ts >= %s", sql)
        self.assertIn("ts < %s", sql)
        self.assertEqual(query_params[1], _WINDOW_START)
        self.assertEqual(query_params[2], _WINDOW_END)
        self.assertEqual(query_params[3], _REPO_SHORT)
        self.assertEqual(query_params[-1], 10)

    def test_other_event_filter_skips_query(self) -> None:
        # If the operator deselects `agent_exit` from the events
        # multiselect, the recent-runs widget logically has no rows
        # -- it is by definition about `agent_exit`. Short-circuit
        # without touching the DB.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_recent_agent_exits(
            events=[_STAGE_ENTER], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_agent_exit_filter_runs_query(self) -> None:
        # Selection includes `agent_exit`; the SQL still hard-AND's
        # `event = 'agent_exit'` and the function returns rows.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        ts = datetime(_YEAR, 5, 25, tzinfo=timezone.utc)
        conn.rows_for = {
            "ORDER BY ts DESC LIMIT %s": [
                (ts, _REPO_SHORT, 7, _STAGE_IMPLEMENTING, "dev", "claude",
                 33.0, 0, False, 1, 0, 100, 200, 0.12, "cli"),
            ],
        }
        rows = analytics_read.get_recent_agent_exits(
            events=[_AGENT_EXIT, _STAGE_ENTER], stages=[_STAGE_IMPLEMENTING],
            connect=conn.as_connect,
        )
        self.assertEqual(len(rows), 1)
        sql, _ = conn.first_query
        self.assertIn("event = %s", sql)
        self.assertIn("stage IN (%s)", sql)

    def test_empty_stage_filter_short_circuits(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_recent_agent_exits(
            stages=[], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])


class IssueEventsFilterTest(unittest.TestCase):
    """The drill-down accepts the same window / event / stage filters
    so the per-issue trace stays consistent with the dashboard above.
    """

    def test_window_and_events_threaded(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        analytics_read.get_issue_events(
            repo=_REPO_SHORT, issue=7,
            start=_WINDOW_START, end=_WINDOW_END, events=[_AGENT_EXIT],
            connect=conn.as_connect,
        )
        sql, query_params = conn.first_query
        self.assertIn("ts >= %s", sql)
        self.assertIn("ts < %s", sql)
        self.assertIn("event IN (%s)", sql)
        self.assertEqual(query_params[0], _REPO_SHORT)
        self.assertEqual(query_params[1], 7)
        self.assertEqual(query_params[2], _WINDOW_START)
        self.assertEqual(query_params[3], _WINDOW_END)
        self.assertEqual(query_params[4], _AGENT_EXIT)

    def test_empty_events_short_circuits(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_issue_events(
            repo=_REPO_SHORT, issue=7, events=[], connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])


if __name__ == "__main__":
    unittest.main()
