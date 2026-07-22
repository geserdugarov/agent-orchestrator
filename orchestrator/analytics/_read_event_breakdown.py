# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Raw event-breakdown query projection."""

from __future__ import annotations

from orchestrator.analytics.predicates import _WindowFilters, _build_window_where
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import EventBreakdown


def _event_breakdown_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[EventBreakdown]:
    where, bindings = _build_window_where(filters)
    rows = query.select(
        f"SELECT event, COUNT(*) AS c FROM analytics_events{where} GROUP BY event ORDER BY c DESC, event ASC",
        bindings,
    )
    return [EventBreakdown(event=event, count=int(count)) for event, count in rows]
