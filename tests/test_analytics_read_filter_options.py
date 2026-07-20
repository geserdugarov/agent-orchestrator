# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

# The unioned filter-options query tags each distinct value with the
# column it came from; tests register the fake's rows against this
# leg of the union and read the bucketed dropdown values back.
_DIM_UNION = "UNION SELECT 'event' AS dim"
_DIM_REPO = "repo"
_REPO_A = "owner/a"
_REPO_B = "owner/b"


class FilterOptionsTest(unittest.TestCase):
    """Filter dropdown population: distinct sorted strings per column,
    empty when nothing is configured."""

    def test_returns_empty_when_db_url_unset(self) -> None:
        analytics_read = _reload_read(db_url="")
        connected = []
        filter_options = analytics_read.get_filter_options(
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(filter_options, analytics_read.FilterOptions())

    def test_sentinel_off_is_unset(self) -> None:
        analytics_read = _reload_read(db_url="off")
        filter_options = analytics_read.get_filter_options(
            connect=lambda url: _FakeConnection(),
        )
        self.assertEqual(filter_options, analytics_read.FilterOptions())

    def test_collects_distinct_values_per_column(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # Layer 3 collapses the five SELECT DISTINCTs into one
        # unioned query; rows are tagged with the column they belong
        # to and the reader buckets them in Python. Fixtures emit
        # values in arbitrary order so the in-Python sort that
        # preserves the previous ascending semantics is exercised.
        conn.rows_for = {
            _DIM_UNION: [
                (_DIM_REPO, _REPO_B), (_DIM_REPO, _REPO_A),
                ("event", "stage_enter"), ("event", "agent_exit"),
                ("stage", "validating"), ("stage", "implementing"),
                ("backend", "codex"), ("backend", "claude"),
                ("agent_role", "review"), ("agent_role", "dev"),
            ],
        }
        filter_options = analytics_read.get_filter_options(connect=conn.as_connect)
        self.assertEqual(filter_options.repos, (_REPO_A, _REPO_B))
        self.assertEqual(filter_options.events, ("agent_exit", "stage_enter"))
        self.assertEqual(filter_options.stages, ("implementing", "validating"))
        self.assertEqual(filter_options.backends, ("claude", "codex"))
        self.assertEqual(filter_options.agent_roles, ("dev", "review"))
        # One unioned query covers all five columns.
        self.assertEqual(len(conn.executed), 1)
        sql, _ = conn.first_query
        # Each leg excludes NULLs via its own WHERE clause -- the
        # union keeps the partial-scan plan per column.
        self.assertEqual(sql.count("IS NOT NULL"), 5)
        # Connection is closed once because there is now one query.
        self.assertEqual(conn.close_called, 1)

    def test_drops_null_rows(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        # A row whose `value` is NULL would not be returned by the
        # SQL (each leg filters `IS NOT NULL`), but the Python
        # bucketer also guards against NULL so a driver that decides
        # to surface a stray NULL never blows up the reader.
        conn.rows_for = {
            _DIM_UNION: [
                (_DIM_REPO, _REPO_A),
                (_DIM_REPO, None),
                (_DIM_REPO, _REPO_B),
            ],
        }
        filter_options = analytics_read.get_filter_options(connect=conn.as_connect)
        self.assertEqual(filter_options.repos, (_REPO_A, _REPO_B))

    def test_empty_rows_yield_empty_filter_options(self) -> None:
        # An empty table (or empty post-filter union) returns the
        # zero-valued `FilterOptions` rather than raising. Mirrors
        # the previous per-column path's empty-result behavior.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {}
        filter_options = analytics_read.get_filter_options(connect=conn.as_connect)
        self.assertEqual(filter_options, analytics_read.FilterOptions())
        self.assertEqual(len(conn.executed), 1)

    def test_unknown_dim_rows_are_ignored(self) -> None:
        # A row whose `dim` is not one of the five known columns
        # (a forward-compat scenario where the SQL gains a leg the
        # reader has not learned about yet) is dropped rather than
        # routed to a stray bucket. Keeps the bucket dict bounded
        # to the dataclass's documented fields.
        analytics_read = _reload_read()
        conn = _FakeConnection()
        conn.rows_for = {
            _DIM_UNION: [
                (_DIM_REPO, _REPO_A),
                ("model", "claude-4-7"),
            ],
        }
        filter_options = analytics_read.get_filter_options(connect=conn.as_connect)
        self.assertEqual(filter_options.repos, (_REPO_A,))


if __name__ == "__main__":
    unittest.main()
