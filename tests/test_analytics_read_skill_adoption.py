# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

# Event predicate the reader pins, and the multiselect value that excludes it.
_EVENT_AGENT_EXIT = "event = 'agent_exit'"
_STAGE_ENTER = "stage_enter"

# The window scan selects the incidental columns; the history scan selects
# `skills_available`. Each substring is unique to its query, so the fake
# cursor routes the two `agent_exit` scans to their own canned rows
# regardless of registration order.
_WINDOW_SCAN = "skills_incidental"
_HISTORY_SCAN = "skills_available"

# Backend / role / skill / repo labels threaded through the fixtures.
_CLAUDE = "claude"
_DEVELOPER = "developer"
_UNKNOWN = "unknown"
_REPO = "owner/repo"
_DEVELOP = "develop"
_REVIEW = "review"

_WINDOW_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
_WINDOW_END = datetime(2026, 6, 24, tzinfo=timezone.utc)


def _window_row(
    *,
    row_id: int,
    repo: str = _REPO,
    role: str = _DEVELOPER,
    backend: str = _CLAUDE,
    resume: str | None = None,
    session: str | None = None,
    triggered: list[str] | None = None,
    triggered_count: int | None = None,
    incidental: list[str] | None = None,
    incidental_count: int | None = None,
) -> tuple:
    """A reporting-window `agent_exit` scan row (diagnostics + identity)."""
    return (
        repo, role, backend, resume, session, row_id,
        triggered, triggered_count, incidental, incidental_count,
    )


def _history_row(
    *,
    row_id: int,
    repo: str = _REPO,
    role: str = _DEVELOPER,
    backend: str = _CLAUDE,
    resume: str | None = None,
    session: str | None = None,
    available: list[str] | None = None,
    triggered: list[str] | None = None,
) -> tuple:
    """A before-window-end `agent_exit` scan row (availability + loads)."""
    return (repo, role, backend, resume, session, row_id, available, triggered)


class SkillAdoptionTest(unittest.TestCase):
    """`get_skill_adoption` aggregates skill use by logical agent session.

    It selects active sessions from the reporting-window `agent_exit`
    rows, reads their availability / load evidence from every `agent_exit`
    row before the window end (ignoring the window start and stage filter),
    counts adoption once per session against the per-session
    `skills_available` denominator, and keeps invocation loads and
    incidental references as window-scoped diagnostics.
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
                    events=events, connect=conn.as_connect,
                )
                self.assertEqual(rows, [])
                self.assertEqual(conn.executed, [])

    def test_adoption_round_trip(self) -> None:
        conn = _FakeConnection()
        window_rows: list[tuple] = []
        history_rows: list[tuple] = []
        row_id = 0
        # 36 sessions that each loaded `develop` in a single window run.
        for index in range(36):
            row_id += 1
            session = f"single-{index}"
            window_rows.append(_window_row(
                row_id=row_id, session=session,
                triggered=[_DEVELOP], triggered_count=3,
            ))
            history_rows.append(_history_row(
                row_id=row_id, session=session,
                available=[_DEVELOP], triggered=[_DEVELOP],
            ))
        # One session split across two resumed runs (shared resume id): still
        # one adopting session, but two load rows and its own invocations.
        for index in range(2):
            row_id += 1
            window_rows.append(_window_row(
                row_id=row_id, resume="resume-1", session=f"resume-{index}",
                triggered=[_DEVELOP], triggered_count=7,
            ))
            history_rows.append(_history_row(
                row_id=row_id, resume="resume-1", session=f"resume-{index}",
                available=[_DEVELOP], triggered=[_DEVELOP],
            ))
        # 4 sessions offered `develop` but that never reached for it.
        for index in range(4):
            row_id += 1
            session = f"available-{index}"
            window_rows.append(_window_row(row_id=row_id, session=session))
            history_rows.append(_history_row(
                row_id=row_id, session=session, available=[_DEVELOP],
            ))
        conn.rows_for = {
            _WINDOW_SCAN: window_rows,
            _HISTORY_SCAN: history_rows,
        }
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row.repo, row.skill, row.agent_role, row.backend),
            (_REPO, _DEVELOP, _DEVELOPER, _CLAUDE),
        )
        # 37 of 41 sessions adopted `develop` across 122 invocations logged
        # over 38 load rows (the resume session contributes two load rows and
        # 14 invocations but one adopting session).
        self.assertEqual(row.sessions, 41)
        self.assertEqual(row.adopted, 37)
        self.assertEqual(row.invocations, 122)
        self.assertEqual(row.load_rows, 38)
        self.assertAlmostEqual(row.adoption_rate, 37 / 41)

    def test_pre_window_load_counts_as_adoption(self) -> None:
        conn = _FakeConnection()
        # A run before the window loaded `develop`; the in-window run resumed
        # it (so both are one logical session) but loaded nothing itself.
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(row_id=2, resume="sess-A", session="sess-B"),
            ],
            _HISTORY_SCAN: [
                _history_row(
                    row_id=1, session="sess-A",
                    available=[_DEVELOP], triggered=[_DEVELOP],
                ),
                _history_row(
                    row_id=2, resume="sess-A", session="sess-B",
                    available=[_DEVELOP],
                ),
            ],
        }
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # The prior-run load keeps the session adopted, yet it stays out of
        # the window-scoped diagnostics.
        self.assertEqual((row.sessions, row.adopted), (1, 1))
        self.assertEqual((row.invocations, row.load_rows), (0, 0))

    def test_history_scan_drops_start_and_stage_keeps_end(self) -> None:
        conn = _FakeConnection()
        _reload_read().get_skill_adoption(
            start=_WINDOW_START, end=_WINDOW_END, repo=_REPO,
            stages=["implementing"], connect=conn.as_connect,
        )
        window_sql, _ = conn.executed[0]
        history_sql, history_params = conn.executed[1]
        # The window scan is fully bounded and stage-filtered.
        self.assertIn("ts >= %s", window_sql)
        self.assertIn("ts < %s", window_sql)
        self.assertIn("stage IN", window_sql)
        # The history scan keeps the end bound so a later load cannot leak
        # backward, but drops the start bound and the stage filter so a
        # prior-stage / pre-window load stays visible.
        self.assertIn("ts < %s", history_sql)
        self.assertNotIn("ts >= %s", history_sql)
        self.assertNotIn("stage IN", history_sql)
        self.assertIn(_WINDOW_END, history_params)
        self.assertNotIn(_WINDOW_START, history_params)

    def test_resume_rows_count_as_one_session(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(
                    row_id=1, resume="r", session="a",
                    triggered=[_DEVELOP], triggered_count=2,
                ),
                _window_row(
                    row_id=2, resume="r", session="b",
                    triggered=[_DEVELOP], triggered_count=3,
                ),
            ],
            _HISTORY_SCAN: [
                _history_row(
                    row_id=1, resume="r", session="a",
                    available=[_DEVELOP], triggered=[_DEVELOP],
                ),
                _history_row(
                    row_id=2, resume="r", session="b",
                    available=[_DEVELOP], triggered=[_DEVELOP],
                ),
            ],
        }
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Two runs sharing a resume id are one adopting session, but their
        # window invocations and load rows both still count.
        self.assertEqual((row.sessions, row.adopted), (1, 1))
        self.assertEqual((row.load_rows, row.invocations), (2, 5))

    def test_idless_rows_are_distinct_sessions(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(row_id=1, triggered=[_DEVELOP], triggered_count=1),
                _window_row(row_id=2, triggered=[_DEVELOP], triggered_count=1),
                _window_row(row_id=3),
            ],
            _HISTORY_SCAN: [
                _history_row(row_id=1, available=[_DEVELOP], triggered=[_DEVELOP]),
                _history_row(row_id=2, available=[_DEVELOP], triggered=[_DEVELOP]),
                _history_row(row_id=3, available=[_DEVELOP]),
            ],
        }
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Every ID-less row is its own session rather than merged into one
        # anonymous bucket, so three distinct sessions, two of them adopting.
        self.assertEqual((row.sessions, row.adopted), (3, 2))
        self.assertEqual(row.load_rows, 2)

    def test_legacy_load_implies_availability_without_metadata(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(
                    row_id=1, session="legacy",
                    triggered=[_DEVELOP], triggered_count=1,
                ),
                _window_row(row_id=2, session="quiet"),
            ],
            _HISTORY_SCAN: [
                _history_row(row_id=1, session="legacy", triggered=[_DEVELOP]),
                _history_row(row_id=2, session="quiet"),
            ],
        }
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # A load with no availability metadata implies the skill was offered,
        # so it counts in the denominator; the metadata-less quiet session
        # with no load fabricates no availability.
        self.assertEqual((row.skill, row.sessions, row.adopted), (_DEVELOP, 1, 1))

    def test_incidental_reference_is_window_scoped_diagnostic(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(
                    row_id=1, session="s1",
                    triggered=[_DEVELOP], triggered_count=2,
                    incidental=[_REVIEW], incidental_count=4,
                ),
            ],
            _HISTORY_SCAN: [
                _history_row(
                    row_id=1, session="s1",
                    available=[_DEVELOP], triggered=[_DEVELOP],
                ),
            ],
        }
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        by_skill = {row.skill: row for row in rows}
        develop = by_skill[_DEVELOP]
        self.assertEqual(
            (develop.sessions, develop.adopted, develop.invocations,
             develop.load_rows, develop.incidental),
            (1, 1, 2, 1, 0),
        )
        # A path-only reference never becomes availability or adoption, but
        # its own diagnostic row stays visible.
        review = by_skill[_REVIEW]
        self.assertEqual(
            (review.sessions, review.adopted, review.incidental),
            (0, 0, 4),
        )

    def test_null_role_and_backend_bucket_unknown(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                (_REPO, None, None, None, "s1", 1, [_DEVELOP], 1, None, None),
            ],
            _HISTORY_SCAN: [
                (_REPO, None, None, None, "s1", 1, [_DEVELOP], [_DEVELOP]),
            ],
        }
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        self.assertEqual((rows[0].agent_role, rows[0].backend), (_UNKNOWN, _UNKNOWN))

    def test_window_and_repo_params_bound(self) -> None:
        conn = _FakeConnection()
        _reload_read().get_skill_adoption(
            start=_WINDOW_START, end=_WINDOW_END, repo=_REPO,
            connect=conn.as_connect,
        )
        window_sql, window_params = conn.executed[0]
        self.assertIn("FROM analytics_events", window_sql)
        self.assertIn(_EVENT_AGENT_EXIT, window_sql)
        self.assertIn("ts >= %s", window_sql)
        self.assertIn("repo = %s", window_sql)
        for value in (_WINDOW_START, _WINDOW_END, _REPO):
            self.assertIn(value, window_params)
        # Neither scan touches the rollup / agent-runs view.
        for sql, _ in conn.executed:
            self.assertIn(_EVENT_AGENT_EXIT, sql)
            self.assertNotIn("analytics_daily_rollup", sql)
            self.assertNotIn("analytics_agent_runs", sql)


if __name__ == "__main__":
    unittest.main()
