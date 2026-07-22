# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Review-round cost query and row projection."""

from __future__ import annotations

from typing import Any, Sequence

from orchestrator.analytics import _read_query_rows as query_rows
from orchestrator.analytics.predicates import (
    _WindowFilters,
    _append_where_condition,
    _build_view_window_where,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import ReviewRoundBucketRow

_AGENT_CACHE_TOKENS_SQL = (
    "(COALESCE(cached_tokens, 0) + COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0))"
)
_AGENT_ALL_TOKENS_SQL = (
    "(COALESCE(input_tokens, 0) "
    "+ COALESCE(output_tokens, 0) "
    "+ COALESCE(cache_read_tokens, 0) "
    "+ COALESCE(cache_write_tokens, 0))"
)
_AGENT_CACHE_FRACTION_SQL = (
    f"CASE WHEN {_AGENT_ALL_TOKENS_SQL} = 0 THEN 0 "
    f"ELSE {_AGENT_CACHE_TOKENS_SQL}::numeric "
    f"/ {_AGENT_ALL_TOKENS_SQL}::numeric END"
)


def _review_round_sql(where: str) -> str:
    return (
        "SELECT "
        "CASE "
        "WHEN review_round IS NULL "
        "AND agent_role = 'developer' "
        "AND stage = 'implementing' THEN '0' "
        "WHEN review_round IS NULL THEN 'unknown' "
        "WHEN review_round <= 0 THEN '0' "
        "WHEN review_round >= 6 THEN '6+' "
        "ELSE review_round::text "
        "END AS bucket, "
        "COUNT(*) AS runs, "
        "SUM(CASE WHEN failed THEN 1 ELSE 0 END) AS failed_runs, "
        "COALESCE(SUM(cost_usd), 0) AS bucket_cost_usd, "
        "SUM(CASE WHEN agent_role = 'developer' THEN 1 ELSE 0 END) "
        "AS developer_runs, "
        "SUM(CASE WHEN agent_role = 'reviewer' THEN 1 ELSE 0 END) "
        "AS reviewer_runs, "
        "COALESCE(SUM(CASE WHEN agent_role = 'developer' "
        "THEN cost_usd ELSE 0 END), 0) AS developer_cost_usd, "
        "COALESCE(SUM(CASE WHEN agent_role = 'reviewer' "
        "THEN cost_usd ELSE 0 END), 0) AS reviewer_cost_usd, "
        "COALESCE(SUM(CASE WHEN agent_role = 'developer' "
        f"THEN COALESCE(cost_usd, 0) * ({_AGENT_CACHE_FRACTION_SQL}) "
        "ELSE 0 END), 0) AS developer_cache_cost_usd, "
        "COALESCE(SUM(CASE WHEN agent_role = 'developer' "
        f"THEN COALESCE(cost_usd, 0) * (1 - ({_AGENT_CACHE_FRACTION_SQL})) "
        "ELSE 0 END), 0) AS developer_no_cache_cost_usd, "
        "COALESCE(SUM(CASE WHEN agent_role = 'reviewer' "
        f"THEN COALESCE(cost_usd, 0) * ({_AGENT_CACHE_FRACTION_SQL}) "
        "ELSE 0 END), 0) AS reviewer_cache_cost_usd, "
        "COALESCE(SUM(CASE WHEN agent_role = 'reviewer' "
        f"THEN COALESCE(cost_usd, 0) * (1 - ({_AGENT_CACHE_FRACTION_SQL})) "
        "ELSE 0 END), 0) AS reviewer_no_cache_cost_usd "
        f"FROM analytics_agent_runs{where} "
        "GROUP BY bucket "
        "ORDER BY runs DESC, bucket ASC"
    )


def _review_round_from_row(row: Sequence[Any]) -> ReviewRoundBucketRow:
    query_row = query_rows.review_round_row(row)
    return ReviewRoundBucketRow(
        bucket=str(query_row.bucket),
        runs=int(query_row.runs or 0),
        failed=int(query_row.failed or 0),
        total_cost_usd=float(query_row.total_cost_usd or 0),
        developer_runs=int(query_row.developer_runs or 0),
        reviewer_runs=int(query_row.reviewer_runs or 0),
        developer_cost_usd=float(query_row.developer_cost_usd or 0),
        reviewer_cost_usd=float(query_row.reviewer_cost_usd or 0),
        developer_cache_cost_usd=float(
            query_row.developer_cache_cost_usd or 0,
        ),
        developer_no_cache_cost_usd=float(
            query_row.developer_no_cache_cost_usd or 0,
        ),
        reviewer_cache_cost_usd=float(
            query_row.reviewer_cache_cost_usd or 0,
        ),
        reviewer_no_cache_cost_usd=float(
            query_row.reviewer_no_cache_cost_usd or 0,
        ),
    )


def _review_round_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[ReviewRoundBucketRow]:
    view_where, view_bindings = _build_view_window_where(filters)
    view_where = _append_where_condition(
        view_where,
        "agent_role IN ('developer', 'reviewer')",
    )
    rows = query.select(_review_round_sql(view_where), view_bindings)
    return [_review_round_from_row(row) for row in rows]
