# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics skill-adoption routing tests."""

import unittest


from datetime import datetime, timezone


from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)


_SESSION_ONE = 's1'


_WINDOW_START_YEAR = 2026


_WINDOW_END_YEAR = 2026


_EVENT_AGENT_EXIT = "event = 'agent_exit'"


_STAGE_ENTER = "stage_enter"


_WINDOW_SCAN = "skills_incidental"


_HISTORY_SCAN = "skills_available"


_UNKNOWN = "unknown"


_REPO = "owner/repo"


_DEVELOP = "develop"


_DEVELOP_ONLY = (_DEVELOP,)


_WINDOW_START = datetime(_WINDOW_START_YEAR, 6, 1, tzinfo=timezone.utc)


_WINDOW_END = datetime(_WINDOW_END_YEAR, 6, 24, tzinfo=timezone.utc)


class SkillAdoptionRoutingTest(unittest.TestCase):
    """`get_skill_adoption` aggregates skill use by logical agent session.

    It selects active sessions from the reporting-window `agent_exit`
    rows, reads their availability / load evidence from every `agent_exit`
    row before the window end (ignoring the window start and stage filter),
    counts adoption once per session against the per-session
    `skills_available` denominator, and keeps the cohort's window run count
    and per-skill load / incidental rows as window-scoped diagnostics.
    """

    def test_unset_db_url_returns_empty(self) -> None:
        analytics_read = _reload_read(db_url="")
        self.assertEqual(
            analytics_read.get_skill_adoption(
                connect=lambda url: _FakeConnection(),
            ),
            [],
        )

    def test_excluded_events_short_circuit(self) -> None:
        analytics_read = _reload_read()
        # An events multiselect that excludes agent_exit -- narrowed to
        # another kind, or cleared entirely -- must skip both scans.
        for events in ([_STAGE_ENTER], []):
            with self.subTest(events=events):
                conn = _FakeConnection()
                rows = analytics_read.get_skill_adoption(
                    events=events,
                    connect=conn.as_connect,
                )
                self.assertEqual(rows, [])
                self.assertEqual(conn.executed, [])

    def test_null_role_and_backend_bucket_unknown(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                (_REPO, None, None, None, _SESSION_ONE, 1, _DEVELOP_ONLY, None),
            ],
            _HISTORY_SCAN: [
                (_REPO, None, None, None, _SESSION_ONE, 1, _DEVELOP_ONLY, True, _DEVELOP_ONLY),
            ],
        }
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        self.assertEqual(
            (rows[0].agent_role, rows[0].backend),
            (_UNKNOWN, _UNKNOWN),
        )

    def test_window_and_repo_params_bound(self) -> None:
        conn = _FakeConnection()
        _reload_read().get_skill_adoption(
            start=_WINDOW_START,
            end=_WINDOW_END,
            repo=_REPO,
            connect=conn.as_connect,
        )
        window_sql, window_params = conn.executed[0]
        self.assertIn("FROM analytics_events", window_sql)
        self.assertIn(_EVENT_AGENT_EXIT, window_sql)
        self.assertIn("ts >= %s", window_sql)
        self.assertIn("repo = %s", window_sql)
        for expected_parameter in (_WINDOW_START, _WINDOW_END, _REPO):
            self.assertIn(expected_parameter, window_params)
        # Neither scan touches the rollup / agent-runs view.
        for sql, _ in conn.executed:
            self.assertIn(_EVENT_AGENT_EXIT, sql)
            self.assertNotIn("analytics_daily_rollup", sql)
            self.assertNotIn("analytics_agent_runs", sql)
