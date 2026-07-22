# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics skill-adoption session tests."""

import unittest


from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)


_ROLE_VALUE = 'r'


_WINDOW_SCAN = "skills_incidental"


_HISTORY_SCAN = "skills_available"


_CLAUDE = "claude"


_DEVELOPER = "developer"


_REPO = "owner/repo"


_DEVELOP = "develop"


_DEVELOP_ONLY = (_DEVELOP,)


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


class SkillAdoptionSessionTest(unittest.TestCase):
    """`get_skill_adoption` aggregates skill use by logical agent session.

    It selects active sessions from the reporting-window `agent_exit`
    rows, reads their availability / load evidence from every `agent_exit`
    row before the window end (ignoring the window start and stage filter),
    counts adoption once per session against the per-session
    `skills_available` denominator, and keeps the cohort's window run count
    and per-skill load / incidental rows as window-scoped diagnostics.
    """

    def test_resume_rows_count_as_one_session(self) -> None:
        conn = _FakeConnection()
        conn.rows_for = {
            _WINDOW_SCAN: [
                _window_row(
                    row_id=1,
                    resume=_ROLE_VALUE,
                    session="a",
                    triggered=_DEVELOP_ONLY,
                ),
                _window_row(
                    row_id=2,
                    resume=_ROLE_VALUE,
                    session="b",
                    triggered=_DEVELOP_ONLY,
                ),
            ],
            _HISTORY_SCAN: [
                _history_row(
                    row_id=1,
                    resume=_ROLE_VALUE,
                    session="a",
                    available=_DEVELOP_ONLY,
                    triggered=_DEVELOP_ONLY,
                ),
                _history_row(
                    row_id=2,
                    resume=_ROLE_VALUE,
                    session="b",
                    available=_DEVELOP_ONLY,
                    triggered=_DEVELOP_ONLY,
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
                _window_row(row_id=1, triggered=_DEVELOP_ONLY),
                _window_row(row_id=2, triggered=_DEVELOP_ONLY),
                _window_row(row_id=3),
            ],
            _HISTORY_SCAN: [
                _history_row(row_id=1, available=_DEVELOP_ONLY, triggered=_DEVELOP_ONLY),
                _history_row(row_id=2, available=_DEVELOP_ONLY, triggered=_DEVELOP_ONLY),
                _history_row(row_id=3, available=_DEVELOP_ONLY),
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
                _window_row(row_id=1, session="legacy", triggered=_DEVELOP_ONLY),
                _window_row(row_id=2, session="quiet"),
            ],
            _HISTORY_SCAN: [
                _history_row(row_id=1, session="legacy", triggered=_DEVELOP_ONLY),
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
                _window_row(row_id=1, session="explicit-empty", triggered=_DEVELOP_ONLY),
                _window_row(row_id=2, session="offered", triggered=_DEVELOP_ONLY),
            ],
            _HISTORY_SCAN: [
                # Explicit "scanned, found none": the key is present but the
                # array is empty, so the load must NOT imply availability.
                _history_row(
                    row_id=1,
                    session="explicit-empty",
                    available=[],
                    triggered=_DEVELOP_ONLY,
                ),
                _history_row(
                    row_id=2,
                    session="offered",
                    available=_DEVELOP_ONLY,
                    triggered=_DEVELOP_ONLY,
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
