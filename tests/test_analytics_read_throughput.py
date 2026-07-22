# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import date

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)
from tests.analytics_assertions import (
    assert_row_fields,
    assert_sql_fragments,
)

_TS_DAY_DAY = 25
_TS_NEXT_DAY_DAY = 26

# The reader's resolved/rejected CASE arm is its most distinctive SQL
# fragment, so tests register the fake's rows against it and read the
# day-bucketed throughput back.
_RESOLVED_CASE = "WHEN stage = 'done'"
_REJECTED = "rejected"

# Two adjacent days the fixtures bucket their counts under.
_YEAR = 2026
_TS_DAY = date(_YEAR, 5, _TS_DAY_DAY)
_TS_NEXT_DAY = date(_YEAR, 5, _TS_NEXT_DAY_DAY)


class ThroughputBreakdownTest(unittest.TestCase):
    """`get_throughput_breakdown` counts `stage_enter` rows whose
    stage is `done` (resolved) or `rejected`, grouped by day. It
    honors the standard event / stage filter contract by
    short-circuiting when the operator excludes the rows it would
    otherwise count."""

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_throughput_breakdown(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_other_event_filter_skips_query(self) -> None:
        # If `stage_enter` is not in the events selection, this
        # widget has nothing to count -- it is by definition about
        # `stage_enter` rows.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_throughput_breakdown(
            events=["agent_exit"],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_events_short_circuits(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_throughput_breakdown(
            events=[],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_empty_stages_short_circuits(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_throughput_breakdown(
            stages=[],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_stage_filter_excludes_done_and_rejected(self) -> None:
        # The operator selected only non-terminal stages -- nothing
        # to count.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        rows = analytics_read.get_throughput_breakdown(
            stages=["implementing", "validating"],
            connect=conn.as_connect,
        )
        self.assertEqual(rows, [])
        self.assertEqual(conn.executed, [])

    def test_returns_per_day_resolved_rejected(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _RESOLVED_CASE: [
                (_TS_DAY, 3, 1),
                (_TS_NEXT_DAY, 5, 0),
            ],
        }
        rows = _reload_read().get_throughput_breakdown(
            connect=conn.as_connect,
        )
        self.assertEqual(len(rows), 2)
        assert_row_fields(
            self,
            rows[0],
            {"day": _TS_DAY, "resolved": 3, _REJECTED: 1},
        )
        assert_row_fields(
            self,
            rows[1],
            {"resolved": 5, _REJECTED: 0},
        )
        sql, query_params = conn.first_query
        # Implicit `event = 'stage_enter'` predicate plus the
        # stage IN ('done', 'rejected') intersection.
        assert_sql_fragments(self, sql, ("event = %s", "stage IN"))
        self.assertTrue(
            {"stage_enter", "done", _REJECTED}.issubset(query_params)
        )

    def test_stage_filter_intersects_terminal_pair(self) -> None:
        # User picked `done` only; SQL must narrow to `stage = 'done'`
        # (via stage IN ('done',)) inside the implicit event filter.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {_RESOLVED_CASE: [(_TS_DAY, 1, 0)]}
        rows = analytics_read.get_throughput_breakdown(
            stages=["done", "implementing"],
            connect=conn.as_connect,
        )
        self.assertEqual(len(rows), 1)
        _, query_params = conn.first_query
        # `implementing` is not in the resolved/rejected pair so it
        # is dropped from the IN clause -- only `done` lands.
        self.assertIn("done", query_params)
        self.assertNotIn(_REJECTED, query_params)
        self.assertNotIn("implementing", query_params)
