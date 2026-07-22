# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Recent agent-exit row query and projection."""

from __future__ import annotations

from typing import Any, Sequence

from orchestrator.analytics import _read_query_rows as query_rows
from orchestrator.analytics._read_raw_values import (
    _bool_or_none,
    _empty_filter_selected,
    _float_or_none,
    _int_or_none,
)
from orchestrator.analytics.predicates import (
    _WindowFilters,
    _agent_event_excluded,
    _build_window_where,
    _prepend_where_condition,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import AgentExitRow


def _agent_exit_from_row(row: Sequence[Any]) -> AgentExitRow:
    query_row = query_rows.agent_exit_row(row)
    return AgentExitRow(
        ts=query_row.ts,
        repo=query_row.repo,
        issue=int(query_row.issue),
        stage=query_row.stage,
        agent_role=query_row.agent_role,
        backend=query_row.backend,
        duration_s=_float_or_none(query_row.duration_s),
        exit_code=_int_or_none(query_row.exit_code),
        timed_out=_bool_or_none(query_row.timed_out),
        review_round=_int_or_none(query_row.review_round),
        retry_count=_int_or_none(query_row.retry_count),
        input_tokens=_int_or_none(query_row.input_tokens),
        output_tokens=_int_or_none(query_row.output_tokens),
        cost_usd=_float_or_none(query_row.cost_usd),
        cost_source=query_row.cost_source,
    )


def _recent_agent_exit_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
    limit: int,
) -> list[AgentExitRow]:
    if _agent_event_excluded(filters.events):
        return []
    if _empty_filter_selected(filters.stages):
        return []
    where, bindings = _build_window_where(filters.without_events())
    where = _prepend_where_condition(where, "event = %s")
    bindings.insert(0, "agent_exit")
    bindings.append(int(limit))
    rows = query.select(
        "SELECT ts, repo, issue, stage, agent_role, backend, "
        "duration_s, exit_code, timed_out, review_round, retry_count, "
        "input_tokens, output_tokens, cost_usd, cost_source "
        f"FROM analytics_events{where} "
        "ORDER BY ts DESC LIMIT %s",
        bindings,
    )
    return [_agent_exit_from_row(row) for row in rows]
