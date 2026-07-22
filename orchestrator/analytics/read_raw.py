# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Raw analytics readers with typed request binding and focused query leaves."""

from __future__ import annotations

from typing import Any

from orchestrator.analytics._read_agent_exits import (
    _agent_exit_from_row as _agent_exit_from_row,
    _recent_agent_exit_rows as _recent_agent_exit_rows,
)
from orchestrator.analytics._read_event_breakdown import (
    _event_breakdown_rows as _event_breakdown_rows,
)
from orchestrator.analytics._read_filter_options import (
    _FILTER_OPTION_COLUMNS as _FILTER_OPTION_COLUMNS,
    _filter_options_from_rows as _filter_options_from_rows,
    _filter_options_sql as _filter_options_sql,
)
from orchestrator.analytics._read_issue_events import (
    _issue_event_from_row as _issue_event_from_row,
    _issue_event_rows as _issue_event_rows,
)
from orchestrator.analytics._read_issues import (
    ISSUE_SORT_BY_OPTIONS as _ISSUE_SORT_BY_OPTIONS,
    SORT_BY_COST as SORT_BY_COST,
    SORT_BY_LAST_SEEN as SORT_BY_LAST_SEEN,
    _issue_order_sql as _issue_order_sql,
    _issue_summary_from_row as _issue_summary_from_row,
    _issue_summary_rows as _issue_summary_rows,
    _issues_sql as _issues_sql,
)
from orchestrator.analytics._read_raw_values import (
    _bool_or_none as _bool_or_none,
    _empty_filter_selected as _empty_filter_selected,
    _float_or_none as _float_or_none,
    _int_or_none as _int_or_none,
    _row_int as _row_int,
)
from orchestrator.analytics.read_models import (
    AgentExitRow,
    DataExtent,
    EventBreakdown,
    FilterOptions,
    IssueEventRow,
    IssueSummaryRow,
)
from orchestrator.analytics.read_request import (
    FILTERED_READ_SIGNATURE,
    ISSUES_SIGNATURE,
    ISSUE_EVENTS_SIGNATURE,
    RECENT_EXITS_SIGNATURE,
    SOURCE_READ_SIGNATURE,
    bind_read_request,
    resolve_read_query,
    window_filters,
)


_COMPATIBILITY_EXPORTS = (
    _agent_exit_from_row,
    _FILTER_OPTION_COLUMNS,
    _issue_event_from_row,
    SORT_BY_COST,
    _issue_order_sql,
    _issue_summary_from_row,
    _issues_sql,
    _bool_or_none,
    _float_or_none,
    _int_or_none,
    _row_int,
)


_FILTER_OPTIONS_SIGNATURE = SOURCE_READ_SIGNATURE.replace(
    return_annotation="FilterOptions",
)
_DATA_EXTENT_SIGNATURE = SOURCE_READ_SIGNATURE.replace(
    return_annotation="DataExtent",
)
_EVENT_BREAKDOWN_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="list[EventBreakdown]",
)
_RECENT_AGENT_EXITS_SIGNATURE = RECENT_EXITS_SIGNATURE.replace(
    return_annotation="list[AgentExitRow]",
)
_ISSUES_READ_SIGNATURE = ISSUES_SIGNATURE.replace(
    return_annotation="list[IssueSummaryRow]",
)
_ISSUE_EVENTS_READ_SIGNATURE = ISSUE_EVENTS_SIGNATURE.replace(
    return_annotation="list[IssueEventRow]",
)


def get_filter_options(*args: Any, **kwargs: Any) -> FilterOptions:
    """Return distinct values populating the dashboard filters."""
    request = bind_read_request(_FILTER_OPTIONS_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return FilterOptions()
    return _filter_options_from_rows(query.select(_filter_options_sql()))


get_filter_options.__signature__ = _FILTER_OPTIONS_SIGNATURE


def get_data_extent(*args: Any, **kwargs: Any) -> DataExtent:
    """Return the minimum and maximum recorded event timestamps."""
    request = bind_read_request(_DATA_EXTENT_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return DataExtent()
    rows = query.select(
        "SELECT MIN(ts) AS data_min_ts, MAX(ts) AS data_max_ts FROM analytics_events",
    )
    if not rows:
        return DataExtent()
    min_ts, max_ts = rows[0]
    return DataExtent(min_ts=min_ts, max_ts=max_ts)


get_data_extent.__signature__ = _DATA_EXTENT_SIGNATURE


def get_event_breakdown(*args: Any, **kwargs: Any) -> list[EventBreakdown]:
    """Return per-event counts inside the selected window."""
    request = bind_read_request(_EVENT_BREAKDOWN_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    return _event_breakdown_rows(query, window_filters(request))


get_event_breakdown.__signature__ = _EVENT_BREAKDOWN_SIGNATURE


def get_recent_agent_exits(
    *args: Any,
    **kwargs: Any,
) -> list[AgentExitRow]:
    """Return the newest filtered agent-exit rows."""
    request = bind_read_request(_RECENT_AGENT_EXITS_SIGNATURE, args, kwargs)
    selected_limit = int(request.options.limit or 0)
    if selected_limit <= 0:
        return []
    query = resolve_read_query(request)
    if not query.available:
        return []
    return _recent_agent_exit_rows(
        query,
        window_filters(request),
        selected_limit,
    )


get_recent_agent_exits.__signature__ = _RECENT_AGENT_EXITS_SIGNATURE


def get_issues(*args: Any, **kwargs: Any) -> list[IssueSummaryRow]:
    """Return one aggregate row for each issue in the selected window."""
    request = bind_read_request(_ISSUES_READ_SIGNATURE, args, kwargs)
    sort_by = request.options.sort_by or SORT_BY_LAST_SEEN
    if sort_by not in _ISSUE_SORT_BY_OPTIONS:
        raise ValueError(
            f"unknown sort_by {sort_by!r}; expected one of {sorted(_ISSUE_SORT_BY_OPTIONS)}",
        )
    selected_limit = int(request.options.limit or 0)
    if selected_limit <= 0:
        return []
    query = resolve_read_query(request)
    if not query.available:
        return []
    return _issue_summary_rows(
        query,
        window_filters(request),
        selected_limit,
        sort_by,
    )


get_issues.__signature__ = _ISSUES_READ_SIGNATURE


def get_issue_events(*args: Any, **kwargs: Any) -> list[IssueEventRow]:
    """Return every selected event for one issue, oldest first."""
    request = bind_read_request(_ISSUE_EVENTS_READ_SIGNATURE, args, kwargs)
    filters = request.filters
    if _empty_filter_selected(filters.events):
        return []
    if _empty_filter_selected(filters.stages):
        return []
    query = resolve_read_query(request)
    if not query.available:
        return []
    return _issue_event_rows(
        query,
        window_filters(request, include_identity=False),
        filters.repo,
        filters.issue,
    )


get_issue_events.__signature__ = _ISSUE_EVENTS_READ_SIGNATURE
