# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.analytics_read_helpers import (
    _FakeConnection,
    _connector,
    _reload,
)


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


if __name__ == "__main__":
    unittest.main()
