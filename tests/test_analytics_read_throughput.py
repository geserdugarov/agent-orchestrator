# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import date

from tests.analytics_read_helpers import (
    _FakeConnection,
    _connector,
    _reload,
)


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
