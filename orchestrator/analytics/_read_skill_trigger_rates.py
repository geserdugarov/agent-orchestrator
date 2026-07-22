# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Skill-trigger rate query and row projection."""

from __future__ import annotations

from typing import Any, Sequence

from orchestrator.analytics._read_dashboard_sql import _AGENT_EXIT_CONDITION
from orchestrator.analytics._read_row_values import _row_value
from orchestrator.analytics._read_skill_values import _label_or_unknown
from orchestrator.analytics.predicates import (
    _WindowFilters,
    _append_where_condition,
    _build_window_where,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import SkillTriggerRateRow


def _skill_trigger_rate_sql(clause: str) -> str:
    return (
        "SELECT "
        "COALESCE(agent_role, 'unknown') AS role_label, "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "COUNT(*) AS runs, "
        "COUNT(*) FILTER "
        "  (WHERE extras -> 'skills_triggered' IS NOT NULL) AS skill_runs, "
        "COALESCE(SUM((extras ->> 'skills_triggered_count')::int), 0) "
        "  AS total_triggers "
        f"FROM analytics_events{clause} "
        "GROUP BY role_label, backend_label "
        "ORDER BY skill_runs DESC, runs DESC, role_label ASC, "
        "backend_label ASC"
    )


def _skill_trigger_rate_from_row(row: Sequence[Any]) -> SkillTriggerRateRow:
    return SkillTriggerRateRow(
        agent_role=_label_or_unknown(row[0]),
        backend=_label_or_unknown(row[1]),
        runs=int(row[2] or 0),
        skill_runs=int(_row_value(row, 3) or 0),
        total_triggers=int(_row_value(row, 4) or 0),
    )


def _skill_trigger_rate_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[SkillTriggerRateRow]:
    event_where, event_bindings = _build_window_where(filters.without_events())
    clause = _append_where_condition(event_where, _AGENT_EXIT_CONDITION)
    rows = query.select(_skill_trigger_rate_sql(clause), event_bindings)
    return [_skill_trigger_rate_from_row(row) for row in rows]
