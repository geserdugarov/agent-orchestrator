# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-skill invocation matrix query and aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from orchestrator.analytics._read_dashboard_sql import _AGENT_EXIT_CONDITION
from orchestrator.analytics._read_row_values import _row_value
from orchestrator.analytics._read_skill_values import (
    _as_skill_names,
    _skill_cohort,
    _skill_matrix_order_key,
)
from orchestrator.analytics._read_skill_types import (
    _SkillCohort,
    _SkillMatrixKey,
)
from orchestrator.analytics.predicates import (
    _WindowFilters,
    _append_where_condition,
    _build_window_where,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import SkillTriggerMatrixRow

SKILL_MATRIX_ROW_LIMIT = 100


def _skill_catalog_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[tuple]:
    catalog_where, catalog_bindings = _build_window_where(filters.catalog_scope())
    clause = _append_where_condition(
        catalog_where,
        "event = 'repo_skill_catalog'",
    )
    return query.select(
        f"SELECT repo, extras -> 'skills_available' AS skills_available FROM analytics_events{clause}",
        catalog_bindings,
    )


def _skill_catalog(rows: Sequence[tuple]) -> dict[str, set[str]]:
    catalog: dict[str, set[str]] = {}
    for row in rows:
        if row[0] is None:
            continue
        repo = str(row[0])
        names = _as_skill_names(_row_value(row, 1, None))
        catalog.setdefault(repo, set()).update(names)
    return catalog


def _skill_run_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[tuple]:
    run_where, run_bindings = _build_window_where(filters.without_events())
    clause = _append_where_condition(run_where, _AGENT_EXIT_CONDITION)
    return query.select(
        "SELECT repo, "
        "COALESCE(agent_role, 'unknown') AS role_label, "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "extras -> 'skills_triggered' AS skills_triggered "
        f"FROM analytics_events{clause}",
        run_bindings,
    )


@dataclass
class _SkillMatrixCounts:
    """Run and trigger counts used to assemble the skill matrix."""

    cohort_runs: dict[_SkillCohort, int] = field(default_factory=dict)
    skill_runs: dict[_SkillMatrixKey, int] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: Sequence[tuple]) -> _SkillMatrixCounts:
        counts = cls()
        for row in rows:
            cohort = _skill_cohort(row)
            counts.cohort_runs[cohort] = counts.cohort_runs.get(cohort, 0) + 1
            for skill in set(_as_skill_names(_row_value(row, 3, None))):
                key = (*cohort, skill)
                counts.skill_runs[key] = counts.skill_runs.get(key, 0) + 1
        return counts

    def matrix_keys(
        self,
        catalog: dict[str, set[str]],
    ) -> set[_SkillMatrixKey]:
        keys = set(self.skill_runs)
        for cohort in self.cohort_runs:
            for skill in catalog.get(cohort[0], ()):
                keys.add((*cohort, skill))
        return keys

    def order_key(self, key: _SkillMatrixKey) -> list:
        return _skill_matrix_order_key(
            key,
            counts=self.skill_runs,
            cohort_runs=self.cohort_runs,
        )

    def as_row(self, key: _SkillMatrixKey) -> SkillTriggerMatrixRow:
        repo, role, backend, skill = key
        return SkillTriggerMatrixRow(
            repo=repo,
            skill=skill,
            agent_role=role,
            backend=backend,
            runs=self.cohort_runs.get((repo, role, backend), 0),
            skill_runs=self.skill_runs.get(key, 0),
        )


def _skill_trigger_matrix_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
    limit: int,
) -> list[SkillTriggerMatrixRow]:
    catalog = _skill_catalog(_skill_catalog_rows(query, filters))
    counts = _SkillMatrixCounts.from_rows(_skill_run_rows(query, filters))
    keys = sorted(counts.matrix_keys(catalog), key=counts.order_key)
    if limit > 0:
        keys = keys[:limit]
    return [counts.as_row(key) for key in keys]
