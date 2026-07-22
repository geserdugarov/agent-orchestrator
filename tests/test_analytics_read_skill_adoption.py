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

# The window scan selects the incidental column; the history scan selects
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


def _session_load_count(index: int) -> int:
    if index <= 35:
        return 1
    if index == 36:
        return 2
    return 0


def _window_row(
    *,
    row_id: int,
    repo: str = _REPO,
    role: str = _DEVELOPER,
    backend: str = _CLAUDE,
    resume: str | None = None,
    session: str | None = None,
    triggered: list[str] | None = None,
    incidental: list[str] | None = None,
) -> tuple:
    """A reporting-window `agent_exit` scan row (identity + skill names)."""
    row = [repo, role, backend, resume, session, row_id, triggered, incidental]
    return tuple(row)


def _history_row(
    *,
    row_id: int,
    repo: str = _REPO,
    role: str = _DEVELOPER,
    backend: str = _CLAUDE,
    resume: str | None = None,
    session: str | None = None,
    available: list[str] | None = None,
    available_present: bool | None = None,
    triggered: list[str] | None = None,
) -> tuple:
    """A before-window-end `agent_exit` scan row (availability + loads).

    `available_present` mirrors the SQL `(extras -> 'skills_available') IS
    NOT NULL` key-presence flag; it defaults to "the array is not None" so a
    caller passing `available=[]` models an explicit empty offered-set while
    `available=None` models an absent key.
    """
    if available_present is None:
        available_present = available is not None
    row = [
        repo, role, backend, resume, session, row_id,
        available, available_present, triggered,
    ]
    return tuple(row)


class SkillAdoptionTest(unittest.TestCase):
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
                    events=events, connect=conn.as_connect,
                )
                self.assertEqual(rows, [])
                self.assertEqual(conn.executed, [])

    def test_adoption_round_trip(self) -> None:
        conn = _FakeConnection()
        window_rows: list[tuple] = []
        history_rows: list[tuple] = []
        row_id = 0
        # 41 sessions, each a resume-anchored chain of window runs, for 122
        # window agent_exit invocations in one cohort. 38 of those runs load
        # `develop`, spread over 37 distinct (adopting) sessions.
        for index in range(41):
            anchor = f"anchor-{index}"
            run_count = 3 if index < 40 else 2
            load_count = _session_load_count(index)
            for run in range(run_count):
                row_id += 1
                triggered = [_DEVELOP] if run < load_count else None
                window_rows.append(_window_row(
                    row_id=row_id, resume=anchor, session=f"run-{row_id}",
                    triggered=triggered,
                ))
                history_rows.append(_history_row(
                    row_id=row_id, resume=anchor, session=f"run-{row_id}",
                    available=[_DEVELOP], triggered=triggered,
                ))
        conn.rows_for = {
            _WINDOW_SCAN: window_rows,
            _HISTORY_SCAN: history_rows,
        }
        # The fixture really holds 122 window rows; `invocations` counts them,
        # not manufactured per-load trigger totals.
        self.assertEqual(len(window_rows), 122)
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row.repo, row.skill, row.agent_role, row.backend),
            (_REPO, _DEVELOP, _DEVELOPER, _CLAUDE),
        )
        # 37 of 41 sessions adopted `develop` across 122 window invocations
        # over 38 load rows.
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
        # The prior-run load keeps the session adopted; the window shows its
        # one in-window invocation but no load row.
        self.assertEqual((row.sessions, row.adopted), (1, 1))
        self.assertEqual((row.invocations, row.load_rows), (1, 0))

    def test_history_drops_start_and_keeps_end(self) -> None:
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
                    row_id=1, resume="r", session="a", triggered=[_DEVELOP],
                ),
                _window_row(
                    row_id=2, resume="r", session="b", triggered=[_DEVELOP],
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
        # Two runs sharing a resume id are one adopting session, but both
        # their window invocations and load rows still count.
        self.assertEqual((row.sessions, row.adopted), (1, 1))
        self.assertEqual((row.invocations, row.load_rows), (2, 2))

    def test_idless_rows_are_distinct_sessions(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(row_id=1, triggered=[_DEVELOP]),
                _window_row(row_id=2, triggered=[_DEVELOP]),
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
        # anonymous bucket: three distinct sessions, two of them adopting.
        self.assertEqual((row.sessions, row.adopted), (3, 2))
        self.assertEqual((row.invocations, row.load_rows), (3, 2))

    def test_legacy_load_implies_availability(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(row_id=1, session="legacy", triggered=[_DEVELOP]),
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
        # A load whose session never carried the `skills_available` key
        # implies the skill was offered, so it counts in the denominator;
        # the metadata-less quiet session with no load fabricates nothing.
        self.assertEqual(
            (row.skill, row.sessions, row.adopted),
            (_DEVELOP, 1, 1),
        )

    def test_empty_metadata_blocks_implied_skill(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(row_id=1, session="explicit-empty", triggered=[_DEVELOP]),
                _window_row(row_id=2, session="offered", triggered=[_DEVELOP]),
            ],
            _HISTORY_SCAN: [
                # Explicit "scanned, found none": the key is present but the
                # array is empty, so the load must NOT imply availability.
                _history_row(
                    row_id=1, session="explicit-empty",
                    available=[], triggered=[_DEVELOP],
                ),
                _history_row(
                    row_id=2, session="offered",
                    available=[_DEVELOP], triggered=[_DEVELOP],
                ),
            ],
        }
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Only the genuinely-offered session counts in the denominator; the
        # explicit-empty session's load is still a visible load row.
        self.assertEqual((row.sessions, row.adopted), (1, 1))
        self.assertEqual(row.load_rows, 2)

    def test_incidental_reference_is_window_scoped(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(
                    row_id=1, session="s1",
                    triggered=[_DEVELOP], incidental=[_REVIEW],
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
            (1, 1, 1, 1, 0),
        )
        # A path-only reference never becomes availability or adoption, but
        # its own diagnostic row stays visible with the cohort run count.
        review = by_skill[_REVIEW]
        self.assertEqual(
            (review.sessions, review.adopted, review.load_rows, review.incidental),
            (0, 0, 0, 1),
        )

    def test_null_role_and_backend_bucket_unknown(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                (_REPO, None, None, None, "s1", 1, [_DEVELOP], None),
            ],
            _HISTORY_SCAN: [
                (_REPO, None, None, None, "s1", 1, [_DEVELOP], True, [_DEVELOP]),
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
            start=_WINDOW_START, end=_WINDOW_END, repo=_REPO,
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


if __name__ == "__main__":
    unittest.main()
