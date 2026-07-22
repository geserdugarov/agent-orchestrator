# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-issue event-trace query and row projection."""

from __future__ import annotations

from typing import Any, Sequence

from orchestrator.analytics._read_raw_values import _float_or_none, _int_or_none
from orchestrator.analytics.predicates import (
    _WindowFilters,
    _build_window_where,
    _prepend_where_condition,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import IssueEventRow


def _issue_event_from_row(row: Sequence[Any]) -> IssueEventRow:
    return IssueEventRow(
        ts=row[0],
        event=row[1],
        stage=row[2],
        duration_s=_float_or_none(row[3]),
        event_result=row[4],
        agent_role=row[5],
        backend=row[6],
        exit_code=_int_or_none(row[7]),
        cost_usd=_float_or_none(row[8]),
    )


def _issue_event_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
    repo: str,
    issue: int,
) -> list[IssueEventRow]:
    where, bindings = _build_window_where(filters)
    where = _prepend_where_condition(where, "repo = %s AND issue = %s")
    rows = query.select(
        "SELECT ts, event, stage, duration_s, result, "
        "agent_role, backend, exit_code, cost_usd "
        f"FROM analytics_events{where} "
        "ORDER BY ts ASC, id ASC",
        [repo, int(issue), *bindings],
    )
    return [_issue_event_from_row(row) for row in rows]
