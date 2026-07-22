# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Logical-session skill evidence queries and normalization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from orchestrator.analytics._read_dashboard_sql import _AGENT_EXIT_CONDITION
from orchestrator.analytics._read_row_values import _row_value
from orchestrator.analytics._read_skill_values import (
    _as_skill_names,
    _skill_cohort,
)
from orchestrator.analytics._read_skill_types import (
    _SESSION_ID_INDEX,
    _SESSION_RESUME_INDEX,
    _SESSION_ROW_INDEX,
    _SkillCohort,
)
from orchestrator.analytics.predicates import (
    _WindowFilters,
    _append_where_condition,
    _build_window_where,
)
from orchestrator.analytics.query import _ReadQuery


def _skill_session_key(row: Sequence[Any]) -> str:
    """Identify a row's logical session: resume id, then session id, then row.

    A resumed run continues the session it resumed *from*, so the
    `resume_session_id` groups a continuation with its origin; a fresh run
    keys on its own `session_id`. A row carrying neither (an older record,
    or a CLI hiccup that yielded no id) falls back to its primary key so
    every ID-less row stays its own session -- never silently merged into a
    single anonymous bucket. The primary key is stable across the window
    and history scans, so a shared ID-less row keys the same in both.
    """
    resume = _row_value(row, _SESSION_RESUME_INDEX, None)
    if isinstance(resume, str) and resume:
        return resume
    session = _row_value(row, _SESSION_ID_INDEX, None)
    if isinstance(session, str) and session:
        return session
    return f"row:{_row_value(row, _SESSION_ROW_INDEX, None)}"


@dataclass
class _SessionEvidence:
    """One logical session's availability + load evidence before window end.

    `available` unions every `skills_available` set the session reported;
    `has_availability_meta` records whether any row carried the
    `skills_available` *key* at all -- tracked by JSON key presence, not by
    a non-empty array, so an explicit `skills_available: []` ("scanned,
    found none") still registers as metadata. `adopted` unions the skills
    the session loaded across its rows. All three are set-based, so folding
    the same row twice (a window row is also returned by the history scan)
    never double-counts.
    """

    available: set[str] = field(default_factory=set)
    adopted: set[str] = field(default_factory=set)
    has_availability_meta: bool = False

    def observe(
        self,
        *,
        available: Sequence[str],
        available_present: bool,
        triggered: Sequence[str],
    ) -> None:
        # `available_present` is the JSON key presence, kept apart from the
        # parsed names: an explicit empty `skills_available` is metadata that
        # blocks the legacy-load fallback, while an absent key is not.
        if available_present:
            self.has_availability_meta = True
        self.available.update(available)
        self.adopted.update(triggered)

    def resolved_available(self) -> set[str]:
        """Skills that count toward this session's denominator.

        The reported `skills_available` union when the session carried any
        availability metadata; otherwise the loaded skills themselves -- a
        legacy load recorded before availability metadata existed implies
        the skill was offered, so it still counts in the denominator. An
        explicit empty `skills_available` is metadata, so it does *not* fall
        back: a load against a session that reported no offered skills does
        not fabricate availability.
        """
        if self.has_availability_meta:
            return self.available
        return set(self.adopted)


@dataclass(frozen=True)
class _SkillWindowRun:
    """One reporting-window `agent_exit` row's session + skill fields."""

    session_key: str
    cohort: _SkillCohort
    triggered: frozenset[str]
    incidental: frozenset[str]


def _skill_window_run(row: Sequence[Any]) -> _SkillWindowRun:
    return _SkillWindowRun(
        session_key=_skill_session_key(row),
        cohort=_skill_cohort(row),
        triggered=frozenset(_as_skill_names(_row_value(row, 6, None))),
        incidental=frozenset(_as_skill_names(_row_value(row, 7, None))),
    )


def _skill_window_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[_SkillWindowRun]:
    window_where, window_bindings = _build_window_where(filters.without_events())
    clause = _append_where_condition(window_where, _AGENT_EXIT_CONDITION)
    rows = query.select(
        "SELECT repo, "
        "COALESCE(agent_role, 'unknown') AS role_label, "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "resume_session_id, session_id, id, "
        "extras -> 'skills_triggered' AS skills_triggered, "
        "extras -> 'skills_incidental' AS skills_incidental "
        f"FROM analytics_events{clause}",
        window_bindings,
    )
    return [_skill_window_run(row) for row in rows]


def _skill_history_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[tuple]:
    history_where, history_bindings = _build_window_where(
        filters.historical_scope(),
    )
    clause = _append_where_condition(history_where, _AGENT_EXIT_CONDITION)
    return query.select(
        "SELECT repo, "
        "COALESCE(agent_role, 'unknown') AS role_label, "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "resume_session_id, session_id, id, "
        "extras -> 'skills_available' AS skills_available, "
        "(extras -> 'skills_available') IS NOT NULL AS has_skills_available, "
        "extras -> 'skills_triggered' AS skills_triggered "
        f"FROM analytics_events{clause}",
        history_bindings,
    )


def _skill_session_evidence(
    query: _ReadQuery,
    filters: _WindowFilters,
    window_runs: Sequence[_SkillWindowRun],
) -> dict[str, _SessionEvidence]:
    """Gather each active session's before-window-end availability + loads.

    Seeds one `_SessionEvidence` per window session (a window row is itself
    evidence observed before the end) so only sessions active in the window
    are tracked, then folds in the history scan -- every `agent_exit` row
    for those sessions before the window end, ignoring the window start and
    stage filter -- so a load from a prior stage or from before the window
    stays visible. History rows for sessions not seen in the window are
    dropped: their evidence must not leak into the aggregate.
    """
    evidence: dict[str, _SessionEvidence] = {}
    for run in window_runs:
        evidence.setdefault(run.session_key, _SessionEvidence()).observe(
            available=(),
            available_present=False,
            triggered=run.triggered,
        )
    for row in _skill_history_rows(query, filters):
        session = evidence.get(_skill_session_key(row))
        if session is None:
            continue
        session.observe(
            available=_as_skill_names(_row_value(row, 6, None)),
            available_present=bool(_row_value(row, 7, False)),
            triggered=_as_skill_names(_row_value(row, 8, None)),
        )
    return evidence
