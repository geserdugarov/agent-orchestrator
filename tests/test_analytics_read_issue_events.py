# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics per-issue event read tests."""

import unittest


from datetime import datetime, timezone


from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)


_NOON_TS_DAY = 25


_NOON_TS_HOUR = 12


_TS_LATER_DAY = 25


_TS_LATER_HOUR = 12


_ROWS_REPO_ISSUE_DURATION_S = 42.0


_ROWS_REPO_ISSUE_COST_USD = 0.05


_AGENT_EXIT = "agent_exit"


_STAGE_ENTER = "stage_enter"


_STAGE_IMPLEMENTING = "implementing"


_REPO_SHORT = "owner/r"


_BACKEND_CLAUDE = "claude"


_AGENT_ROLE_DEV = "dev"


_YEAR = 2026


_NOON_TS = datetime(_YEAR, 5, _NOON_TS_DAY, _NOON_TS_HOUR, 0, tzinfo=timezone.utc)


class IssueEventsTest(unittest.TestCase):
    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_issue_events(
                repo=_REPO_SHORT,
                issue=1,
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_returns_rows_for_repo_issue(self) -> None:
        analytics_read = _reload_read()
        conn = _FakeConnection()
        ts_later = datetime(_YEAR, 5, _TS_LATER_DAY, _TS_LATER_HOUR, 5, tzinfo=timezone.utc)
        conn.rows_for = {
            "WHERE repo = %s AND issue = %s": [
                (_NOON_TS, _STAGE_ENTER, _STAGE_IMPLEMENTING, None, None, None, None, None, None),
                (ts_later, _AGENT_EXIT, _STAGE_IMPLEMENTING, 42.0, None, _AGENT_ROLE_DEV, _BACKEND_CLAUDE, 0, 0.05),
            ],
        }
        rows = analytics_read.get_issue_events(
            repo=_REPO_SHORT,
            issue=7,
            connect=conn.as_connect,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].event, _STAGE_ENTER)
        self.assertEqual(rows[0].stage, _STAGE_IMPLEMENTING)
        self.assertEqual(rows[1].event, _AGENT_EXIT)
        self.assertEqual(rows[1].duration_s, _ROWS_REPO_ISSUE_DURATION_S)
        self.assertEqual(rows[1].backend, _BACKEND_CLAUDE)
        self.assertEqual(rows[1].cost_usd, _ROWS_REPO_ISSUE_COST_USD)
        # Parameterised, not interpolated.
        _, query_params = conn.first_query
        self.assertEqual(query_params, (_REPO_SHORT, 7))
