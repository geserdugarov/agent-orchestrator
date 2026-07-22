# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Cost coverage, backend tokens, and heatmap projections."""

from __future__ import annotations

from typing import Any, Sequence

from orchestrator.analytics._read_row_values import _day_value, _row_value
from orchestrator.analytics.predicates import (
    _WindowFilters,
    _build_view_window_where,
    _build_window_where,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import (
    BackendDailyTokensRow,
    CostCoverageRow,
    HourlyHeatmapPoint,
)


def _cost_coverage_from_row(row: Sequence[Any]) -> CostCoverageRow:
    return CostCoverageRow(
        cost_source=str(row[0]),
        runs=int(row[1] or 0),
        total_tokens=int(_row_value(row, 2) or 0),
    )


def _cost_coverage_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[CostCoverageRow]:
    coverage_where, coverage_bindings = _build_view_window_where(filters)
    rows = query.select(
        "SELECT "
        "COALESCE(cost_source, 'unknown') AS source_label, "
        "COUNT(*) AS runs, "
        "COALESCE(SUM("
        "  COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + "
        "  COALESCE(cache_read_tokens, 0) + "
        "  COALESCE(cache_write_tokens, 0)"
        "), 0) AS source_total_tokens "
        f"FROM analytics_agent_runs{coverage_where} "
        "GROUP BY source_label "
        "ORDER BY runs DESC, source_label ASC",
        coverage_bindings,
    )
    return [_cost_coverage_from_row(row) for row in rows]


def _backend_daily_tokens_from_row(
    row: Sequence[Any],
) -> BackendDailyTokensRow:
    return BackendDailyTokensRow(
        day=_day_value(row[0]),
        backend=str(row[1]),
        total_tokens=int(row[2] or 0),
    )


def _backend_daily_token_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[BackendDailyTokensRow]:
    daily_where, daily_bindings = _build_view_window_where(filters)
    rows = query.select(
        "SELECT "
        "date_trunc('day', ts)::date AS day, "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "COALESCE(SUM("
        "  COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + "
        "  COALESCE(cache_read_tokens, 0) + "
        "  COALESCE(cache_write_tokens, 0)"
        "), 0) AS day_backend_tokens "
        f"FROM analytics_agent_runs{daily_where} "
        "GROUP BY day, backend_label "
        "ORDER BY day ASC, backend_label ASC",
        daily_bindings,
    )
    return [_backend_daily_tokens_from_row(row) for row in rows]


def _hourly_heatmap_from_row(row: Sequence[Any]) -> HourlyHeatmapPoint:
    return HourlyHeatmapPoint(
        weekday=int(row[0]),
        hour=int(row[1]),
        count=int(row[2] or 0),
        total_tokens=int(_row_value(row, 3) or 0),
    )


def _hourly_heatmap_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
    tz_offset_hours: int,
) -> list[HourlyHeatmapPoint]:
    heatmap_where, heatmap_bindings = _build_window_where(filters)
    offset = int(tz_offset_hours)
    rows = query.select(
        "SELECT "
        "EXTRACT(DOW FROM ((ts AT TIME ZONE 'UTC') "
        "+ %s * INTERVAL '1 hour'))::int AS weekday, "
        "EXTRACT(HOUR FROM ((ts AT TIME ZONE 'UTC') "
        "+ %s * INTERVAL '1 hour'))::int AS hour, "
        "COUNT(*) AS c, "
        "COALESCE(SUM("
        "  COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + "
        "  COALESCE(cache_read_tokens, 0) + "
        "  COALESCE(cache_write_tokens, 0)"
        "), 0) AS cell_total_tokens "
        f"FROM analytics_events{heatmap_where} "
        "GROUP BY weekday, hour "
        "ORDER BY weekday ASC, hour ASC",
        [offset, offset, *heatmap_bindings],
    )
    return [_hourly_heatmap_from_row(row) for row in rows]
