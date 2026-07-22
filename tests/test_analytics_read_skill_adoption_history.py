# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics skill-adoption history tests."""

import unittest


from dataclasses import dataclass, field


from datetime import datetime, timezone


from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)


_SESSION_ONE = 's1'


_WINDOW_START_YEAR = 2026


_WINDOW_END_YEAR = 2026


_SESSION_LOAD_COUNT_EXPECTED = 35


_SESSION_LOAD_COUNT_EXPECTED_SECONDARY = 36


_ADOPTION_ROUND_TRIP_RANGE_ARGUMENT = 41


_RUN_COUNT = 40


_ADOPTION_ROUND_TRIP_WINDOW_ROWS_COUNT = 122


_ADOPTION_ROUND_TRIP_SESSIONS = 41


_ADOPTION_ROUND_TRIP_ADOPTED = 37


_ADOPTION_ROUND_TRIP_INVOCATIONS = 122


_ADOPTION_ROUND_TRIP_LOAD_ROWS = 38


_ADOPTION_ROUND_TRIP_ADOPTION_RATE = 37


_ADOPTION_ROUND_TRIP_ADOPTION_RAT_SECONDARY = 41


_WINDOW_SCAN = "skills_incidental"


_HISTORY_SCAN = "skills_available"


_CLAUDE = "claude"


_DEVELOPER = "developer"


_REPO = "owner/repo"


_DEVELOP = "develop"


_REVIEW = "review"


_DEVELOP_ONLY = (_DEVELOP,)


_WINDOW_START = datetime(_WINDOW_START_YEAR, 6, 1, tzinfo=timezone.utc)


_WINDOW_END = datetime(_WINDOW_END_YEAR, 6, 24, tzinfo=timezone.utc)


def _session_load_count(index: int) -> int:
    if index <= _SESSION_LOAD_COUNT_EXPECTED:
        return 1
    if index == _SESSION_LOAD_COUNT_EXPECTED_SECONDARY:
        return 2
    return 0


def _window_row(**row_fields: object) -> tuple:
    """A reporting-window `agent_exit` scan row (identity + skill names)."""
    row = [
        row_fields.get("repo", _REPO),
        row_fields.get("role", _DEVELOPER),
        row_fields.get("backend", _CLAUDE),
        row_fields.get("resume"),
        row_fields.get("session"),
        row_fields["row_id"],
        row_fields.get("triggered"),
        row_fields.get("incidental"),
    ]
    return tuple(row)


def _history_row(**row_fields: object) -> tuple:
    """A before-window-end `agent_exit` scan row (availability + loads).

    `available_present` mirrors the SQL `(extras -> 'skills_available') IS
    NOT NULL` key-presence flag; it defaults to "the array is not None" so a
    caller passing `available=[]` models an explicit empty offered-set while
    `available=None` models an absent key.
    """
    available = row_fields.get("available")
    available_present = row_fields.get("available_present")
    if available_present is None:
        available_present = available is not None
    row = [
        row_fields.get("repo", _REPO),
        row_fields.get("role", _DEVELOPER),
        row_fields.get("backend", _CLAUDE),
        row_fields.get("resume"),
        row_fields.get("session"),
        row_fields["row_id"],
        available,
        available_present,
        row_fields.get("triggered"),
    ]
    return tuple(row)


@dataclass
class _AdoptionRoundTripFixture:
    window_rows: list[tuple] = field(default_factory=list)
    history_rows: list[tuple] = field(default_factory=list)
    _row_id: int = 0

    def add_session(self, index: int) -> None:
        """Add one resume-anchored session and its invocation rows."""
        anchor = f"anchor-{index}"
        run_count = 3 if index < _RUN_COUNT else 2
        load_count = _session_load_count(index)
        for run in range(run_count):
            self._row_id += 1
            triggered = _DEVELOP_ONLY if run < load_count else None
            self.window_rows.append(
                _window_row(
                    row_id=self._row_id,
                    resume=anchor,
                    session=f"run-{self._row_id}",
                    triggered=triggered,
                )
            )
            self.history_rows.append(
                _history_row(
                    row_id=self._row_id,
                    resume=anchor,
                    session=f"run-{self._row_id}",
                    available=_DEVELOP_ONLY,
                    triggered=triggered,
                )
            )


class SkillAdoptionHistoryTest(unittest.TestCase):
    """`get_skill_adoption` aggregates skill use by logical agent session.

    It selects active sessions from the reporting-window `agent_exit`
    rows, reads their availability / load evidence from every `agent_exit`
    row before the window end (ignoring the window start and stage filter),
    counts adoption once per session against the per-session
    `skills_available` denominator, and keeps the cohort's window run count
    and per-skill load / incidental rows as window-scoped diagnostics.
    """

    def test_adoption_round_trip(self) -> None:
        conn = _FakeConnection()
        fixture = _AdoptionRoundTripFixture()
        # 41 sessions, each a resume-anchored chain of window runs, for 122
        # window agent_exit invocations in one cohort. 38 of those runs load
        # `develop`, spread over 37 distinct (adopting) sessions.
        for index in range(_ADOPTION_ROUND_TRIP_RANGE_ARGUMENT):
            fixture.add_session(index)
        conn.rows_for = {
            _WINDOW_SCAN: fixture.window_rows,
            _HISTORY_SCAN: fixture.history_rows,
        }
        # The fixture really holds 122 window rows; `invocations` counts them,
        # not manufactured per-load trigger totals.
        self.assertEqual(len(fixture.window_rows), _ADOPTION_ROUND_TRIP_WINDOW_ROWS_COUNT)
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row.repo, row.skill, row.agent_role, row.backend),
            (_REPO, _DEVELOP, _DEVELOPER, _CLAUDE),
        )
        # 37 of 41 sessions adopted `develop` across 122 window invocations
        # over 38 load rows.
        self.assertEqual(row.sessions, _ADOPTION_ROUND_TRIP_SESSIONS)
        self.assertEqual(row.adopted, _ADOPTION_ROUND_TRIP_ADOPTED)
        self.assertEqual(row.invocations, _ADOPTION_ROUND_TRIP_INVOCATIONS)
        self.assertEqual(row.load_rows, _ADOPTION_ROUND_TRIP_LOAD_ROWS)
        self.assertAlmostEqual(
            row.adoption_rate, _ADOPTION_ROUND_TRIP_ADOPTION_RATE / _ADOPTION_ROUND_TRIP_ADOPTION_RAT_SECONDARY
        )

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
                    row_id=1,
                    session="sess-A",
                    available=_DEVELOP_ONLY,
                    triggered=_DEVELOP_ONLY,
                ),
                _history_row(
                    row_id=2,
                    resume="sess-A",
                    session="sess-B",
                    available=_DEVELOP_ONLY,
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
            start=_WINDOW_START,
            end=_WINDOW_END,
            repo=_REPO,
            stages=["implementing"],
            connect=conn.as_connect,
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

    def test_incidental_reference_is_window_scoped(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(
                    row_id=1,
                    session=_SESSION_ONE,
                    triggered=_DEVELOP_ONLY,
                    incidental=[_REVIEW],
                ),
            ],
            _HISTORY_SCAN: [
                _history_row(
                    row_id=1,
                    session=_SESSION_ONE,
                    available=_DEVELOP_ONLY,
                    triggered=_DEVELOP_ONLY,
                ),
            ],
        }
        rows = _reload_read().get_skill_adoption(connect=conn.as_connect)
        by_skill = {row.skill: row for row in rows}
        develop = by_skill[_DEVELOP]
        self.assertEqual(
            (develop.sessions, develop.adopted, develop.invocations, develop.load_rows, develop.incidental),
            (1, 1, 1, 1, 0),
        )
        # A path-only reference never becomes availability or adoption, but
        # its own diagnostic row stays visible with the cohort run count.
        review = by_skill[_REVIEW]
        self.assertEqual(
            (review.sessions, review.adopted, review.load_rows, review.incidental),
            (0, 0, 0, 1),
        )
