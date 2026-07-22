# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Rollup analytics readers with typed requests and focused projections."""

from __future__ import annotations

from typing import Any

from orchestrator.analytics._read_rollup_breakdowns import (
    _backend_efficiency_from_row as _backend_efficiency_from_row,
    _backend_efficiency_rows as _backend_efficiency_rows,
    _backend_efficiency_sql as _backend_efficiency_sql,
    _stage_breakdown_from_row as _stage_breakdown_from_row,
    _stage_breakdown_rows as _stage_breakdown_rows,
    _stage_breakdown_sql as _stage_breakdown_sql,
)
from orchestrator.analytics._read_rollup_cost_sql import (
    _ROLLUP_ALL_TOKENS_SQL as _ROLLUP_ALL_TOKENS_SQL,
    _ROLLUP_CACHE_FRACTION_SQL as _ROLLUP_CACHE_FRACTION_SQL,
    _ROLLUP_CACHE_TOKENS_SQL as _ROLLUP_CACHE_TOKENS_SQL,
)
from orchestrator.analytics._read_rollup_repo import (
    _THROUGHPUT_RESOLVED_STAGES as _THROUGHPUT_RESOLVED_STAGES,
    _repo_breakdown_rows as _repo_breakdown_rows,
    _selected_throughput_stages as _selected_throughput_stages,
    _throughput_from_row as _throughput_from_row,
    _throughput_rows as _throughput_rows,
)
from orchestrator.analytics._read_rollup_series import (
    _kpi_prev_sql as _kpi_prev_sql,
    _kpi_prev_summary as _kpi_prev_summary,
    _time_series_from_row as _time_series_from_row,
    _time_series_rows as _time_series_rows,
)
from orchestrator.analytics._read_row_values import (
    _cost_cell as _cost_cell,
    _day_value as _day_value,
    _float_or_none as _float_or_none,
    _row_value as _row_value,
)
from orchestrator.analytics._read_summary_query import (
    _build_summary_sql as _build_summary_sql,
    _build_summary_where as _build_summary_where,
    _query_summary_rows as _query_summary_rows,
)
from orchestrator.analytics._read_summary_result import (
    _SUMMARY_TOTAL_FIELD_CASTS as _SUMMARY_TOTAL_FIELD_CASTS,
    _ordered_summary_counts as _ordered_summary_counts,
    _summary_count_order as _summary_count_order,
    _summary_from_rows as _summary_from_rows,
    _summary_total_values as _summary_total_values,
    _summary_totals_row as _summary_totals_row,
)
from orchestrator.analytics.predicates import _agent_event_excluded
from orchestrator.analytics.read_models import (
    BackendEfficiencyRow,
    RepoBreakdownRow,
    StageBreakdown,
    Summary,
    ThroughputDayRow,
    TimeSeriesPoint,
)
from orchestrator.analytics.read_request import (
    FILTERED_READ_SIGNATURE,
    bind_read_request,
    resolve_read_query,
    window_filters,
)


_COMPATIBILITY_EXPORTS = (
    _backend_efficiency_from_row,
    _backend_efficiency_sql,
    _stage_breakdown_from_row,
    _stage_breakdown_sql,
    _ROLLUP_ALL_TOKENS_SQL,
    _ROLLUP_CACHE_FRACTION_SQL,
    _ROLLUP_CACHE_TOKENS_SQL,
    _THROUGHPUT_RESOLVED_STAGES,
    _selected_throughput_stages,
    _throughput_from_row,
    _kpi_prev_sql,
    _time_series_from_row,
    _cost_cell,
    _day_value,
    _float_or_none,
    _row_value,
    _build_summary_sql,
    _build_summary_where,
    _SUMMARY_TOTAL_FIELD_CASTS,
    _ordered_summary_counts,
    _summary_count_order,
    _summary_total_values,
    _summary_totals_row,
)


_SUMMARY_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="Summary",
)
_KPI_PREVIOUS_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="Summary",
)
_TIME_SERIES_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="list[TimeSeriesPoint]",
)
_STAGE_BREAKDOWN_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="list[StageBreakdown]",
)
_BACKEND_EFFICIENCY_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="list[BackendEfficiencyRow]",
)
_REPO_BREAKDOWN_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="list[RepoBreakdownRow]",
)
_THROUGHPUT_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="list[ThroughputDayRow]",
)


def get_summary(*args: Any, **kwargs: Any) -> Summary:
    """Return aggregate counts for the selected reporting window."""
    request = bind_read_request(_SUMMARY_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return Summary()
    return _summary_from_rows(
        _query_summary_rows(query, window_filters(request)),
    )


get_summary.__signature__ = _SUMMARY_SIGNATURE


def get_kpi_prev(*args: Any, **kwargs: Any) -> Summary:
    """Return previous-window scalar totals used by KPI comparisons."""
    request = bind_read_request(_KPI_PREVIOUS_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return Summary()
    return _kpi_prev_summary(query, window_filters(request))


get_kpi_prev.__signature__ = _KPI_PREVIOUS_SIGNATURE


def get_time_series(*args: Any, **kwargs: Any) -> list[TimeSeriesPoint]:
    """Return daily event, cost, and token aggregates."""
    request = bind_read_request(_TIME_SERIES_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    return _time_series_rows(query, window_filters(request))


get_time_series.__signature__ = _TIME_SERIES_SIGNATURE


def get_stage_breakdown(*args: Any, **kwargs: Any) -> list[StageBreakdown]:
    """Return per-stage activity and cost aggregates."""
    request = bind_read_request(_STAGE_BREAKDOWN_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    return _stage_breakdown_rows(query, window_filters(request))


get_stage_breakdown.__signature__ = _STAGE_BREAKDOWN_SIGNATURE


def get_backend_efficiency(
    *args: Any,
    **kwargs: Any,
) -> list[BackendEfficiencyRow]:
    """Return per-backend agent-run efficiency aggregates."""
    request = bind_read_request(_BACKEND_EFFICIENCY_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    if _agent_event_excluded(request.filters.events):
        return []
    return _backend_efficiency_rows(query, window_filters(request))


get_backend_efficiency.__signature__ = _BACKEND_EFFICIENCY_SIGNATURE


def get_repo_breakdown(*args: Any, **kwargs: Any) -> list[RepoBreakdownRow]:
    """Return per-repository activity aggregates."""
    request = bind_read_request(_REPO_BREAKDOWN_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    return _repo_breakdown_rows(query, window_filters(request))


get_repo_breakdown.__signature__ = _REPO_BREAKDOWN_SIGNATURE


def get_throughput_breakdown(
    *args: Any,
    **kwargs: Any,
) -> list[ThroughputDayRow]:
    """Return daily resolved and rejected issue counts."""
    request = bind_read_request(_THROUGHPUT_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    return _throughput_rows(query, window_filters(request))


get_throughput_breakdown.__signature__ = _THROUGHPUT_SIGNATURE
