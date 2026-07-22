# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Repository and terminal-throughput rollup projections."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional, Sequence

from orchestrator.analytics._read_row_values import _cost_cell, _day_value
from orchestrator.analytics.predicates import (
    _DAILY_ROLLUP_VIEW,
    _WindowFilters,
    _build_rollup_window_where,
    _prepend_where_condition,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import RepoBreakdownRow, ThroughputDayRow


def _repo_breakdown_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[RepoBreakdownRow]:
    where, bindings = _build_rollup_window_where(filters)
    rows = query.select(
        "SELECT repo, "
        "COUNT(DISTINCT issue) AS repo_issues, "
        "COALESCE(SUM(event_count), 0) AS repo_events, "
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN event_count ELSE 0 END), 0) "
        "  AS repo_agent_exits, "
        "COALESCE(SUM(total_cost_usd), 0) AS repo_cost_usd "
        f"FROM {_DAILY_ROLLUP_VIEW}{where} "
        "GROUP BY repo "
        "ORDER BY repo_events DESC, repo ASC",
        bindings,
    )
    return [
        RepoBreakdownRow(
            repo=row[0],
            issues=int(row[1] or 0),
            events=int(row[2] or 0),
            agent_exits=int(row[3] or 0),
            total_cost_usd=_cost_cell(row, 4),
        )
        for row in rows
    ]


# Stages a `stage_enter` event must carry to count as a terminal
# resolution -- `done` means merged / closed successfully,
# `rejected` means closed without merge. Kept private to this module
# because the throughput helper is the only consumer; if a future
# caller needs the same set, promote it to a documented constant.
_THROUGHPUT_RESOLVED_STAGES: tuple[str, ...] = ("done", "rejected")


def _selected_throughput_stages(
    stages: Optional[Sequence[str]],
) -> tuple[str, ...]:
    if stages is None:
        return _THROUGHPUT_RESOLVED_STAGES
    return tuple(stage for stage in stages if stage in _THROUGHPUT_RESOLVED_STAGES)


def _throughput_from_row(row: Sequence[Any]) -> ThroughputDayRow:
    return ThroughputDayRow(
        day=_day_value(row[0]),
        resolved=int(row[1] or 0),
        rejected=int(row[2] or 0),
    )


def _throughput_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[ThroughputDayRow]:
    if filters.events is not None and "stage_enter" not in filters.events:
        return []
    active_stages = _selected_throughput_stages(filters.stages)
    if not active_stages:
        return []
    scoped_filters = replace(filters, events=None, stages=active_stages)
    where, bindings = _build_rollup_window_where(scoped_filters)
    where = _prepend_where_condition(where, "event = %s")
    bindings.insert(0, "stage_enter")
    rows = query.select(
        "SELECT day, "
        "COALESCE(SUM(CASE WHEN stage = 'done' "
        "                  THEN event_count ELSE 0 END), 0) AS resolved, "
        "COALESCE(SUM(CASE WHEN stage = 'rejected' "
        "                  THEN event_count ELSE 0 END), 0) AS rejected "
        f"FROM {_DAILY_ROLLUP_VIEW}{where} "
        "GROUP BY day "
        "ORDER BY day ASC",
        bindings,
    )
    return [_throughput_from_row(row) for row in rows]
