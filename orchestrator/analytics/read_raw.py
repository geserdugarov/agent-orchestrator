# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Raw-table analytics readers over `analytics_events` / `analytics_agent_runs`.

The foundational read helpers that return row-level or simple
overview shapes straight from the base table (or the agent-run
view) without going through the daily rollup: the filter-dropdown
distinct values, the data-extent bounds the date picker defaults
to, the per-event count breakdown, the newest agent-exit rows, the
one-row-per-`(repo, issue)` overview, and the per-issue event
trace.

Re-exported unchanged through `orchestrator.analytics.read`; see
that module's docstring for the connection / URL / error contract
shared across every reader. The rollup-backed aggregates live in
`read_rollup`; the redesigned-dashboard chart breakdowns in
`read_dashboard`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from orchestrator.analytics.predicates import (
    _WindowFilters,
    _agent_event_excluded,
    _build_window_where,
    _prepend_where_condition,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import (
    AgentExitRow,
    DataExtent,
    EventBreakdown,
    FilterOptions,
    IssueEventRow,
    IssueSummaryRow,
)


_FILTER_OPTION_COLUMNS: tuple[str, ...] = (
    "repo", "event", "stage", "backend", "agent_role",
)


def _int_or_none(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    return int(raw)


def _float_or_none(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    return float(raw)


def _row_int(row: Sequence[Any], index: int) -> int:
    if len(row) <= index:
        return 0
    return int(row[index] or 0)


def _bool_or_none(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    return bool(raw)


def _empty_filter_selected(selection: Optional[Sequence[str]]) -> bool:
    if selection is None:
        return False
    return len(selection) == 0


def _filter_options_sql() -> str:
    return " UNION ".join(
        f"SELECT '{column}' AS dim, {column} AS value "
        f"FROM analytics_events WHERE {column} IS NOT NULL"
        for column in _FILTER_OPTION_COLUMNS
    )


def _filter_options_from_rows(rows: Sequence[tuple]) -> FilterOptions:
    buckets: dict[str, list[str]] = {
        column: [] for column in _FILTER_OPTION_COLUMNS
    }
    for row in rows:
        if not row or row[1] is None:
            continue
        dimension = row[0]
        if dimension in buckets:
            buckets[dimension].append(row[1])
    for option_names in buckets.values():
        option_names.sort()
    return FilterOptions(
        repos=tuple(buckets["repo"]),
        events=tuple(buckets["event"]),
        stages=tuple(buckets["stage"]),
        backends=tuple(buckets["backend"]),
        agent_roles=tuple(buckets["agent_role"]),
    )


def get_filter_options(
    *,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
    conn: Any = None,
) -> FilterOptions:
    """Distinct values populating the dashboard filter dropdowns.

    Returns an empty `FilterOptions` when `ANALYTICS_DB_URL` is unset
    or when the table is empty -- the dashboard renders disabled
    dropdowns rather than crashing. Failure to reach the configured
    database raises `AnalyticsReadError`. Pass `conn=` (typically
    from an `analytics_connection` scope) to reuse a connection
    across reads instead of opening a fresh socket.

    The five filter columns are read with one unioned query so the
    dashboard pays a single round-trip instead of five. Each leg is a
    partial scan on its own column; the planner is free to pick an
    unordered union plan because the per-bucket lists get sorted in
    Python after the fetch (the lists are tiny -- at most a few
    hundred values per column).
    """
    query = _ReadQuery.resolve(db_url, connect, conn)
    if not query.available:
        return FilterOptions()
    return _filter_options_from_rows(query.select(_filter_options_sql()))


def get_data_extent(
    *,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
    conn: Any = None,
) -> DataExtent:
    """Min / max `ts` across `analytics_events`.

    The dashboard reads this once at boot to default the sidebar's
    date picker to a window that actually contains data, rather
    than to "today" against a freshly-deployed empty table. Returns
    `DataExtent()` (both fields `None`) when the DB URL is unset or
    the table is empty.
    """
    query = _ReadQuery.resolve(db_url, connect, conn)
    if not query.available:
        return DataExtent()
    rows = query.select(
        "SELECT MIN(ts) AS data_min_ts, MAX(ts) AS data_max_ts "
        "FROM analytics_events",
    )
    if not rows:
        return DataExtent()
    min_ts, max_ts = rows[0]
    return DataExtent(min_ts=min_ts, max_ts=max_ts)


def _event_breakdown_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[EventBreakdown]:
    where, bindings = _build_window_where(filters)
    rows = query.select(
        "SELECT event, COUNT(*) AS c "
        f"FROM analytics_events{where} "
        "GROUP BY event ORDER BY c DESC, event ASC",
        bindings,
    )
    return [
        EventBreakdown(event=event, count=int(count))
        for event, count in rows
    ]


def get_event_breakdown(
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    repo: Optional[str] = None,
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
    conn: Any = None,
) -> list[EventBreakdown]:
    """Per-event counts within the window.

    Mirrors `get_stage_breakdown`'s shape so the dashboard can render
    the two side-by-side without divergent typing.
    """
    query = _ReadQuery.resolve(db_url, connect, conn)
    if not query.available:
        return []
    filters = _WindowFilters(
        start=start,
        end=end,
        repo=repo,
        events=events,
        stages=stages,
        issue=issue,
    )
    return _event_breakdown_rows(query, filters)


def _agent_exit_from_row(row: Sequence[Any]) -> AgentExitRow:
    return AgentExitRow(
        ts=row[0],
        repo=row[1],
        issue=int(row[2]),
        stage=row[3],
        agent_role=row[4],
        backend=row[5],
        duration_s=_float_or_none(row[6]),
        exit_code=_int_or_none(row[7]),
        timed_out=_bool_or_none(row[8]),
        review_round=_int_or_none(row[9]),
        retry_count=_int_or_none(row[10]),
        input_tokens=_int_or_none(row[11]),
        output_tokens=_int_or_none(row[12]),
        cost_usd=_float_or_none(row[13]),
        cost_source=row[14],
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


def get_recent_agent_exits(
    *,
    limit: int = 50,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    repo: Optional[str] = None,
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
    conn: Any = None,
) -> list[AgentExitRow]:
    """The newest agent-exit rows for an overview table.

    `limit` clamps to a positive int (LIMIT 0 returns nothing
    cleanly, but a negative value would be a SQL error -- guard at
    the application layer). Filters to `event='agent_exit'` so the
    table only carries rows whose agent / cost columns are populated.
    `start` / `end` apply the same window the dashboard uses for
    every other widget so the recent-runs table moves with the date
    range. `events` / `stages` / `issue` follow the same shape as in
    the other readers: ``None`` = no filter, empty = no rows match,
    non-empty = ``IN (...)``. The event filter is intersected with
    the hardcoded ``event = 'agent_exit'``, so deselecting
    ``agent_exit`` from the multiselect produces an empty table --
    which is the consistent answer when the operator excludes the
    rows this widget displays.
    """
    query = _ReadQuery.resolve(db_url, connect, conn)
    if limit <= 0:
        return []
    if not query.available:
        return []
    filters = _WindowFilters(
        start=start,
        end=end,
        repo=repo,
        events=events,
        stages=stages,
        issue=issue,
    )
    return _recent_agent_exit_rows(query, filters, limit)


SORT_BY_LAST_SEEN = "last_seen"
SORT_BY_COST = "cost"
_ISSUE_SORT_BY_OPTIONS: frozenset[str] = frozenset(
    {SORT_BY_LAST_SEEN, SORT_BY_COST}
)


def _issue_order_sql(sort_by: str) -> str:
    if sort_by == SORT_BY_COST:
        return (
            "ORDER BY SUM(cost_usd) DESC NULLS LAST, "
            "last_seen DESC, repo ASC, issue ASC"
        )
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
    return IssueSummaryRow(
        repo=row[0],
        issue=int(row[1]),
        event_count=int(row[2] or 0),
        first_seen=row[3],
        last_seen=row[4],
        latest_stage=row[5],
        agent_exits=int(row[6] or 0),
        total_cost_usd=_float_or_none(row[7]),
        total_input_tokens=int(row[8] or 0),
        total_output_tokens=int(row[9] or 0),
        max_review_round=_int_or_none(row[10] if len(row) > 10 else None),
        failed_agent_runs=_row_int(row, 11),
        max_retry_count=_int_or_none(row[12] if len(row) > 12 else None),
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


def get_issues(
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    repo: Optional[str] = None,
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
    limit: int = 100,
    sort_by: str = SORT_BY_LAST_SEEN,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
    conn: Any = None,
) -> list[IssueSummaryRow]:
    """Date / repo-bounded one-row-per-`(repo, issue)` overview.

    Powers the dashboard's "issues" tables: each row aggregates the
    events seen for a single `(repo, issue)` pair inside the window
    (count, first / last activity ts, the most recent non-null stage
    as a "current status" hint, agent-exit count, rolled-up cost
    / token totals, the highest review round any agent run for the
    issue reached, how many of those runs exited non-zero, and the
    highest `retry_count` any run rode up to).

    `sort_by` controls the SQL ordering:

    - `"last_seen"` (default) orders by `MAX(ts) DESC` so the most
      recently active issues surface first -- used by callers that
      want a "latest activity" view.
    - `"cost"` orders by `SUM(cost_usd) DESC NULLS LAST` so the
      highest-cost issues across the entire window surface first
      -- this is what the redesigned "Most expensive issues" panel
      needs. Sorting in-Python after a `last_seen`-ordered LIMIT
      would silently drop older high-cost issues outside the
      truncated set.

    `last_seen DESC, repo ASC, issue ASC` is the deterministic
    tie-breaker in either mode. Unknown `sort_by` raises `ValueError`
    so a typo never silently degrades to last-seen ordering. `limit`
    caps the row count for a bounded dashboard table; non-positive
    values short-circuit to an empty list, matching
    `get_recent_agent_exits`.

    `latest_stage` is computed with
    `(array_agg(stage ORDER BY ts DESC) FILTER (WHERE stage IS NOT NULL))[1]`
    -- a Postgres-native idiom that avoids a correlated subquery and
    stays correct when the most recent event for an issue does not
    carry a stage (e.g. an `agent_exit` after a `stage_evaluation`).
    """
    if sort_by not in _ISSUE_SORT_BY_OPTIONS:
        raise ValueError(
            f"unknown sort_by {sort_by!r}; expected one of "
            f"{sorted(_ISSUE_SORT_BY_OPTIONS)}"
        )
    query = _ReadQuery.resolve(db_url, connect, conn)
    if limit <= 0:
        return []
    if not query.available:
        return []
    filters = _WindowFilters(
        start=start,
        end=end,
        repo=repo,
        events=events,
        stages=stages,
        issue=issue,
    )
    return _issue_summary_rows(query, filters, limit, sort_by)


def _issue_event_from_row(row: Sequence[Any]) -> IssueEventRow:
    return IssueEventRow(
        ts=row[0],
        event=row[1],
        stage=row[2],
        duration_s=_float_or_none(row[3]),
        result=row[4],
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


def get_issue_events(
    *,
    repo: str,
    issue: int,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
    conn: Any = None,
) -> list[IssueEventRow]:
    """Every event for a single `(repo, issue)`, oldest first.

    Powers the per-issue drill-down view. Returns an empty list when
    the DB URL is unset or the (post-filter) issue has no recorded
    events. `repo` is matched exactly (case-sensitive, matching how
    `analytics.build_record` writes it). `start` / `end` apply the
    same window the dashboard uses for every other widget so the
    drill-down narrows along with the sidebar date range. `events`
    / `stages` follow the standard shape: ``None`` = no filter,
    empty = no rows match, non-empty = ``IN (...)``.
    """
    if _empty_filter_selected(events):
        return []
    if _empty_filter_selected(stages):
        return []
    query = _ReadQuery.resolve(db_url, connect, conn)
    if not query.available:
        return []
    filters = _WindowFilters(
        start=start,
        end=end,
        events=events,
        stages=stages,
    )
    return _issue_event_rows(query, filters, repo, issue)
