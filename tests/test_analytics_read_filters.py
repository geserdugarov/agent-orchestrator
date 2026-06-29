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


if __name__ == "__main__":
    unittest.main()
