# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stage and backend rollup query projections."""

from __future__ import annotations

from typing import Any, Sequence

from orchestrator.analytics._read_row_values import (
    _cost_cell,
    _float_or_none,
    _row_value,
)
from orchestrator.analytics._read_rollup_cost_sql import (
    _ROLLUP_CACHE_FRACTION_SQL,
)
from orchestrator.analytics.predicates import (
    _DAILY_ROLLUP_VIEW,
    _WindowFilters,
    _append_where_condition,
    _build_rollup_window_where,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import BackendEfficiencyRow, StageBreakdown


def _stage_breakdown_sql(clause: str) -> str:
    return (
        "SELECT stage, "
        "COALESCE(SUM(event_count), 0) AS c, "
        "SUM(duration_s_sum) / NULLIF(SUM(duration_s_count), 0) "
        "  AS avg_dur, "
        "COALESCE(SUM(total_cost_usd), 0) AS stage_cost_usd, "
        "COALESCE(SUM(total_input_tokens), 0) AS stage_input_tokens, "
        "COALESCE(SUM(total_output_tokens), 0) AS stage_output_tokens, "
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN event_count ELSE 0 END), 0) "
        "  AS stage_agent_runs, "
        "COALESCE(SUM(COALESCE(total_cost_usd, 0) "
        f"* ({_ROLLUP_CACHE_FRACTION_SQL})), 0) AS stage_cache_cost_usd, "
        "COALESCE(SUM(COALESCE(total_cost_usd, 0) "
        f"* (1 - ({_ROLLUP_CACHE_FRACTION_SQL}))), 0) "
        "AS stage_no_cache_cost_usd "
        f"FROM {_DAILY_ROLLUP_VIEW}{clause} "
        "GROUP BY stage ORDER BY c DESC, stage ASC"
    )


def _stage_breakdown_from_row(row: Sequence[Any]) -> StageBreakdown:
    return StageBreakdown(
        stage=row[0],
        count=int(row[1]),
        avg_duration_s=_float_or_none(row[2]),
        total_cost_usd=_cost_cell(row, 3),
        total_input_tokens=int(_row_value(row, 4) or 0),
        total_output_tokens=int(_row_value(row, 5) or 0),
        runs=int(_row_value(row, 6) or 0),
        cache_cost_usd=_cost_cell(row, 7),
        no_cache_cost_usd=_cost_cell(row, 8),
    )


def _stage_breakdown_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[StageBreakdown]:
    where, bindings = _build_rollup_window_where(filters)
    clause = _append_where_condition(where, "stage IS NOT NULL")
    rows = query.select(_stage_breakdown_sql(clause), bindings)
    return [_stage_breakdown_from_row(row) for row in rows]


def _backend_efficiency_sql(clause: str) -> str:
    return (
        "SELECT "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "COALESCE(SUM(event_count), 0) AS runs, "
        "COALESCE(SUM(failed_count), 0) AS failed_runs, "
        "SUM(duration_s_sum) / NULLIF(SUM(duration_s_count), 0) "
        "  AS avg_dur, "
        "COALESCE(SUM(total_cost_usd), 0) AS backend_cost_usd, "
        "COALESCE(SUM(total_input_tokens), 0) AS backend_input_tokens, "
        "COALESCE(SUM(total_output_tokens), 0) AS backend_output_tokens, "
        "COALESCE(SUM(total_cache_read_tokens), 0) "
        "  AS backend_cache_read_tokens, "
        "COALESCE(SUM(total_cache_write_tokens), 0) "
        "  AS backend_cache_write_tokens "
        f"FROM {_DAILY_ROLLUP_VIEW}{clause} "
        "GROUP BY backend_label "
        "ORDER BY runs DESC, backend_label ASC"
    )


def _backend_efficiency_from_row(
    row: Sequence[Any],
) -> BackendEfficiencyRow:
    return BackendEfficiencyRow(
        backend=str(row[0]),
        runs=int(row[1] or 0),
        failed=int(row[2] or 0),
        avg_duration_s=_float_or_none(row[3]),
        total_cost_usd=_cost_cell(row, 4),
        total_input_tokens=int(row[5] or 0),
        total_output_tokens=int(row[6] or 0),
        total_cache_read_tokens=int(_row_value(row, 7) or 0),
        total_cache_write_tokens=int(_row_value(row, 8) or 0),
    )


def _backend_efficiency_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[BackendEfficiencyRow]:
    where, bindings = _build_rollup_window_where(filters.without_events())
    clause = _append_where_condition(where, "event = 'agent_exit'")
    rows = query.select(_backend_efficiency_sql(clause), bindings)
    return [_backend_efficiency_from_row(row) for row in rows]
