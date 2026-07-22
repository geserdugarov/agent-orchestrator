# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-session skill adoption aggregation and result projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from orchestrator.analytics._read_skill_sessions import (
    _SessionEvidence,
    _SkillWindowRun,
    _skill_session_evidence,
    _skill_window_rows,
)
from orchestrator.analytics._read_skill_types import (
    _SkillAdoptionKey,
    _SkillCohort,
)
from orchestrator.analytics.predicates import _WindowFilters
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import SkillAdoptionRow

SKILL_ADOPTION_ROW_LIMIT = 100


@dataclass
class _SkillAdoption:
    """Per-`(repo, role, backend, skill)` session counts and window diagnostics.

    `cohort_runs` is the window `agent_exit` invocation count per
    `(repo, role, backend)` cohort -- every run, whether or not it loaded a
    skill -- so each skill's adoption reads against the cohort's run volume.
    `load_rows` / `incidental` count the window runs that loaded /
    incidentally referenced a given skill.
    """

    cohort_runs: dict[_SkillCohort, int] = field(default_factory=dict)
    sessions: dict[_SkillAdoptionKey, int] = field(default_factory=dict)
    adopted: dict[_SkillAdoptionKey, int] = field(default_factory=dict)
    load_rows: dict[_SkillAdoptionKey, int] = field(default_factory=dict)
    incidental: dict[_SkillAdoptionKey, int] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        window_runs: Sequence[_SkillWindowRun],
        evidence: dict[str, _SessionEvidence],
    ) -> _SkillAdoption:
        counts = cls()
        session_cohorts: dict[str, set[_SkillCohort]] = {}
        for run in window_runs:
            counts._observe_window(run)
            session_cohorts.setdefault(run.session_key, set()).add(run.cohort)
        counts._count_sessions(session_cohorts, evidence)
        return counts

    def keys(self) -> set[_SkillAdoptionKey]:
        # Every available cell plus any cell that only shows in the window
        # diagnostics (a purely incidental reference, or a load whose session
        # reported a different availability set) so no observation is dropped.
        keys = set(self.sessions)
        keys.update(self.load_rows)
        keys.update(self.incidental)
        return keys

    def order_key(self, key: _SkillAdoptionKey) -> list:
        repo, role, backend, skill = key
        return [
            -self.sessions.get(key, 0),
            -self.adopted.get(key, 0),
            -self.cohort_runs.get((repo, role, backend), 0),
            repo,
            role,
            backend,
            skill,
        ]

    def as_row(self, key: _SkillAdoptionKey) -> SkillAdoptionRow:
        repo, role, backend, skill = key
        return SkillAdoptionRow(
            repo=repo,
            skill=skill,
            agent_role=role,
            backend=backend,
            sessions=self.sessions.get(key, 0),
            adopted=self.adopted.get(key, 0),
            invocations=self.cohort_runs.get((repo, role, backend), 0),
            load_rows=self.load_rows.get(key, 0),
            incidental=self.incidental.get(key, 0),
        )

    def _observe_window(self, run: _SkillWindowRun) -> None:
        cohort = run.cohort
        self.cohort_runs[cohort] = self.cohort_runs.get(cohort, 0) + 1
        for skill in run.triggered:
            key = (*cohort, skill)
            self.load_rows[key] = self.load_rows.get(key, 0) + 1
        for skill in run.incidental:
            key = (*cohort, skill)
            self.incidental[key] = self.incidental.get(key, 0) + 1

    def _count_sessions(
        self,
        session_cohorts: dict[str, set[_SkillCohort]],
        evidence: dict[str, _SessionEvidence],
    ) -> None:
        for session_key, cohorts in session_cohorts.items():
            session = evidence.get(session_key)
            if session is None:
                continue
            self._count_session(cohorts, session)

    def _count_session(
        self,
        cohorts: set[_SkillCohort],
        session: _SessionEvidence,
    ) -> None:
        available = session.resolved_available()
        for cohort in cohorts:
            for skill in available:
                key = (*cohort, skill)
                self.sessions[key] = self.sessions.get(key, 0) + 1
                if skill in session.adopted:
                    self.adopted[key] = self.adopted.get(key, 0) + 1


def _skill_adoption_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
    limit: int,
) -> list[SkillAdoptionRow]:
    window_runs = _skill_window_rows(query, filters)
    evidence = _skill_session_evidence(query, filters, window_runs)
    counts = _SkillAdoption.build(window_runs, evidence)
    keys = sorted(counts.keys(), key=counts.order_key)
    if limit > 0:
        keys = keys[:limit]
    return [counts.as_row(key) for key in keys]
