# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Combined rollup summary query construction."""

from __future__ import annotations

from typing import Any

from orchestrator.analytics.predicates import (
    _DAILY_ROLLUP_VIEW,
    _WindowFilters,
    _build_rollup_window_where,
)
from orchestrator.analytics.query import _ReadQuery


def _build_summary_where(
    filters: _WindowFilters,
) -> tuple[str, list[Any]]:
    """Build the predicate and bound values for one summary window."""
    return _build_rollup_window_where(filters)


def _build_summary_sql(where_clause: str) -> str:
    """Build the single rollup query for totals and breakdowns."""
    # The CTE applies the window once while the discriminator keeps totals,
    # event counts, and stage counts in one round-trip. Sorting the breakdowns
    # after the query lets PostgreSQL choose an aggregate plan without an
    # ordering constraint.
    return (
        "WITH win AS ("
        "SELECT event, stage, repo, issue, "
        "event_count, failed_count, timed_out_count, "
        "total_cost_usd, total_input_tokens, total_output_tokens, "
        "total_cache_read_tokens, total_cache_write_tokens "
        f"FROM {_DAILY_ROLLUP_VIEW}{where_clause}"
        ") "
        "SELECT 't' AS kind, NULL::text AS label, "
        "COALESCE(SUM(event_count), 0) AS count_val, "
        # GitHub issue numbers are only unique within a repository.
        "COUNT(DISTINCT (repo, issue)) AS distinct_issues, "
        "COUNT(DISTINCT repo) AS distinct_repos, "
        "COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd, "
        "COALESCE(SUM(total_input_tokens), 0) AS total_input_tokens, "
        "COALESCE(SUM(total_output_tokens), 0) AS total_output_tokens, "
        # Agent-run counters must exclude non-exit events carrying an exit code.
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN event_count ELSE 0 END), 0) "
        "  AS total_agent_runs, "
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN failed_count ELSE 0 END), 0) "
        "  AS failed_agent_runs, "
        "COALESCE(SUM(total_cache_read_tokens), 0) "
        "  AS total_cache_read_tokens, "
        "COALESCE(SUM(total_cache_write_tokens), 0) "
        "  AS total_cache_write_tokens, "
        "COALESCE(SUM(timed_out_count), 0) AS timed_out_agent_runs "
        "FROM win "
        "UNION ALL "
        "SELECT 'e', event, COALESCE(SUM(event_count), 0), "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL "
        "FROM win GROUP BY event "
        "UNION ALL "
        "SELECT 's', stage, COALESCE(SUM(event_count), 0), "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL "
        "FROM win WHERE stage IS NOT NULL GROUP BY stage"
    )


def _query_summary_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[tuple]:
    """Execute one summary query using the requested connection path."""
    where_clause, query_parameters = _build_summary_where(filters)
    query_sql = _build_summary_sql(where_clause)
    return query.select(query_sql, query_parameters)
