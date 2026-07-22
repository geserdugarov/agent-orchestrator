# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Issue-summary query, ordering, and row projection."""

from __future__ import annotations

from typing import Any, Sequence

from orchestrator.analytics import _read_query_rows as query_rows
from orchestrator.analytics._read_raw_values import (
    _float_or_none,
    _int_or_none,
)
from orchestrator.analytics.predicates import _WindowFilters, _build_window_where
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import IssueSummaryRow

SORT_BY_LAST_SEEN = "last_seen"
SORT_BY_COST = "cost"
ISSUE_SORT_BY_OPTIONS: frozenset[str] = frozenset((SORT_BY_LAST_SEEN, SORT_BY_COST))


def _issue_order_sql(sort_by: str) -> str:
    if sort_by == SORT_BY_COST:
        return "ORDER BY SUM(cost_usd) DESC NULLS LAST, last_seen DESC, repo ASC, issue ASC"
    return "ORDER BY last_seen DESC, repo ASC, issue ASC"


def _issues_sql(where: str, sort_by: str) -> str:
    return (
        "SELECT "
        "repo, issue, "
        "COUNT(*) AS event_count, "
        "MIN(ts) AS first_seen, "
        "MAX(ts) AS last_seen, "
        "(array_agg(stage ORDER BY ts DESC) "
        "  FILTER (WHERE stage IS NOT NULL))[1] AS latest_stage, "
        "SUM(CASE WHEN event = 'agent_exit' THEN 1 ELSE 0 END) "
        "  AS agent_exits, "
        "SUM(cost_usd) AS total_cost_usd, "
        "COALESCE(SUM(input_tokens), 0) AS total_input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS total_output_tokens, "
        "MAX(review_round) AS max_review_round, "
        "SUM(CASE WHEN event = 'agent_exit' AND exit_code <> 0 "
        "         THEN 1 ELSE 0 END) AS failed_agent_runs, "
        "MAX(retry_count) AS max_retry_count "
        f"FROM analytics_events{where} "
        "GROUP BY repo, issue "
        f"{_issue_order_sql(sort_by)} "
        "LIMIT %s"
    )


def _issue_summary_from_row(row: Sequence[Any]) -> IssueSummaryRow:
    query_row = query_rows.issue_summary_row(row)
    return IssueSummaryRow(
        repo=query_row.repo,
        issue=int(query_row.issue),
        event_count=int(query_row.event_count or 0),
        first_seen=query_row.first_seen,
        last_seen=query_row.last_seen,
        latest_stage=query_row.latest_stage,
        agent_exits=int(query_row.agent_exits or 0),
        total_cost_usd=_float_or_none(query_row.total_cost_usd),
        total_input_tokens=int(query_row.total_input_tokens or 0),
        total_output_tokens=int(query_row.total_output_tokens or 0),
        max_review_round=_int_or_none(query_row.max_review_round),
        failed_agent_runs=int(query_row.failed_agent_runs or 0),
        max_retry_count=_int_or_none(query_row.max_retry_count),
    )


def _issue_summary_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
    limit: int,
    sort_by: str,
) -> list[IssueSummaryRow]:
    where, bindings = _build_window_where(filters)
    rows = query.select(
        _issues_sql(where, sort_by),
        [*bindings, int(limit)],
    )
    return [_issue_summary_from_row(row) for row in rows]
