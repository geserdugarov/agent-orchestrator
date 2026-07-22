# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics recent agent-exit read tests."""

import unittest


from datetime import datetime, timezone


from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)


from tests.analytics_assertions import (
    assert_row_fields,
)


_NOON_TS_DAY = 25


_NOON_TS_HOUR = 12


_FILTERED_AGENT_EXIT_DURATION_S = 33.0


_FILTERED_AGENT_EXIT_COST_USD = 0.12


_AGENT_EXIT = "agent_exit"


_STAGE_IMPLEMENTING = "implementing"


_REPO_SHORT = "owner/r"


_BACKEND_CLAUDE = "claude"


_AGENT_ROLE_DEV = "dev"


_YEAR = 2026


_NOON_TS = datetime(_YEAR, 5, _NOON_TS_DAY, _NOON_TS_HOUR, 0, tzinfo=timezone.utc)


class RecentAgentExitsTest(unittest.TestCase):
    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_recent_agent_exits(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_non_positive_limit_short_circuits(self) -> None:
        analytics_read = _reload_read()
        connected = []
        exits = analytics_read.get_recent_agent_exits(
            limit=0,
            connect=lambda url: connected.append(url) or _FakeConnection(),
        )
        self.assertEqual(connected, [])
        self.assertEqual(exits, [])

    def test_returns_rows_filtered_to_agent_exit(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            "ORDER BY ts DESC LIMIT %s": [
                (
                    _NOON_TS,
                    _REPO_SHORT,
                    7,
                    _STAGE_IMPLEMENTING,
                    _AGENT_ROLE_DEV,
                    _BACKEND_CLAUDE,
                    33.0,
                    0,
                    False,
                    1,
                    0,
                    100,
                    200,
                    0.12,
                    "cli",
                ),
            ],
        }
        exits = _reload_read().get_recent_agent_exits(
            limit=10,
            repo=_REPO_SHORT,
            connect=conn.as_connect,
        )
        self.assertEqual(len(exits), 1)
        exit_row = exits[0]
        assert_row_fields(
            self,
            exit_row,
            {
                "ts": _NOON_TS,
                "repo": _REPO_SHORT,
                "issue": 7,
                "stage": _STAGE_IMPLEMENTING,
                "agent_role": _AGENT_ROLE_DEV,
                "backend": _BACKEND_CLAUDE,
                "duration_s": _FILTERED_AGENT_EXIT_DURATION_S,
                "exit_code": 0,
                "timed_out": False,
                "cost_usd": _FILTERED_AGENT_EXIT_COST_USD,
                "cost_source": "cli",
            },
        )
        # Query carries event='agent_exit' + repo filter + limit.
        sql, query_params = conn.first_query
        self.assertIn("event = %s", sql)
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(query_params, (_AGENT_EXIT, _REPO_SHORT, 10))
