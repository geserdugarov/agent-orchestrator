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

from .connection import _default_connect
from .db_url import _resolve_db_url
from .predicates import _build_window_where
from .query import _query
from .read_models import (
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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return FilterOptions()
    connect_fn = connect or _default_connect
    sql = " UNION ".join(
        f"SELECT '{col}' AS dim, {col} AS value "
        f"FROM analytics_events WHERE {col} IS NOT NULL"
        for col in _FILTER_OPTION_COLUMNS
    )
    rows = _query(connect_fn, url, sql, conn=conn)
    buckets: dict[str, list[str]] = {
        col: [] for col in _FILTER_OPTION_COLUMNS
    }
    for row in rows:
        if not row or row[1] is None:
            continue
        dim = row[0]
        if dim in buckets:
            buckets[dim].append(row[1])
    for values in buckets.values():
        values.sort()
    return FilterOptions(
        repos=tuple(buckets["repo"]),
        events=tuple(buckets["event"]),
        stages=tuple(buckets["stage"]),
        backends=tuple(buckets["backend"]),
        agent_roles=tuple(buckets["agent_role"]),
    )


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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return DataExtent()
    connect_fn = connect or _default_connect
    rows = _query(
        connect_fn,
        url,
        "SELECT MIN(ts) AS data_min_ts, MAX(ts) AS data_max_ts "
        "FROM analytics_events",
        conn=conn,
    )
    if not rows:
        return DataExtent()
    min_ts, max_ts = rows[0]
    return DataExtent(min_ts=min_ts, max_ts=max_ts)


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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    connect_fn = connect or _default_connect
    where, params = _build_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
    sql = (
        "SELECT event, COUNT(*) AS c "
        f"FROM analytics_events{where} "
        "GROUP BY event ORDER BY c DESC, event ASC"
    )
    rows = _query(connect_fn, url, sql, params, conn=conn)
    return [EventBreakdown(event=ev, count=int(c)) for ev, c in rows]


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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    if limit <= 0:
        return []
    # Operator deselected agent_exit from the events multiselect;
    # this widget is exclusively about agent_exit rows, so short
    # circuit to an empty table without a DB round trip.
    if events is not None and "agent_exit" not in events:
        return []
    connect_fn = connect or _default_connect
    conditions = ["event = %s"]
    params: list[Any] = ["agent_exit"]
    if start is not None:
        conditions.append("ts >= %s")
        params.append(start)
    if end is not None:
        conditions.append("ts < %s")
        params.append(end)
    if repo is not None:
        conditions.append("repo = %s")
        params.append(repo)
    if issue is not None:
        conditions.append("issue = %s")
        params.append(int(issue))
    if stages is not None:
        if not stages:
            return []
        placeholders = ", ".join(["%s"] * len(stages))
        conditions.append(f"stage IN ({placeholders})")
        params.extend(stages)
    where = " WHERE " + " AND ".join(conditions)
    params.append(int(limit))
    sql = (
        "SELECT ts, repo, issue, stage, agent_role, backend, "
        "duration_s, exit_code, timed_out, review_round, retry_count, "
        "input_tokens, output_tokens, cost_usd, cost_source "
        f"FROM analytics_events{where} "
        "ORDER BY ts DESC LIMIT %s"
    )
    rows = _query(connect_fn, url, sql, params, conn=conn)
    out: list[AgentExitRow] = []
    for row in rows:
        (
            ts,
            repo_v,
            issue_v,
            stage,
            agent_role,
            backend,
            duration_s,
            exit_code,
            timed_out,
            review_round,
            retry_count,
            input_tokens,
            output_tokens,
            cost_usd,
            cost_source,
        ) = row
        out.append(
            AgentExitRow(
                ts=ts,
                repo=repo_v,
                issue=int(issue_v),
                stage=stage,
                agent_role=agent_role,
                backend=backend,
                duration_s=float(duration_s) if duration_s is not None else None,
                exit_code=int(exit_code) if exit_code is not None else None,
                timed_out=bool(timed_out) if timed_out is not None else None,
                review_round=(
                    int(review_round) if review_round is not None else None
                ),
                retry_count=(
                    int(retry_count) if retry_count is not None else None
                ),
                input_tokens=(
                    int(input_tokens) if input_tokens is not None else None
                ),
                output_tokens=(
                    int(output_tokens) if output_tokens is not None else None
                ),
                cost_usd=float(cost_usd) if cost_usd is not None else None,
                cost_source=cost_source,
            )
        )
    return out


SORT_BY_LAST_SEEN = "last_seen"
SORT_BY_COST = "cost"
_ISSUE_SORT_BY_OPTIONS: frozenset[str] = frozenset(
    {SORT_BY_LAST_SEEN, SORT_BY_COST}
)


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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    if limit <= 0:
        return []
    connect_fn = connect or _default_connect
    where, params = _build_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
    # Order primary key matches `sort_by`; secondary keys
    # (`last_seen DESC, repo ASC, issue ASC`) keep the ordering
    # deterministic when the primary key ties.
    if sort_by == SORT_BY_COST:
        order_sql = (
            "ORDER BY SUM(cost_usd) DESC NULLS LAST, "
            "last_seen DESC, repo ASC, issue ASC"
        )
    else:
        order_sql = "ORDER BY last_seen DESC, repo ASC, issue ASC"
    sql = (
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
        # `review_round` is only ever set on agent_exit rows so a
        # plain MAX is correct -- the filter is implicit.
        "MAX(review_round) AS max_review_round, "
        "SUM(CASE WHEN event = 'agent_exit' AND exit_code <> 0 "
        "         THEN 1 ELSE 0 END) AS failed_agent_runs, "
        # `retry_count` is also only ever set on agent_exit rows,
        # so a plain MAX picks the highest retry the implementer
        # ever rode up to before the issue cleared. The redesigned
        # "Most expensive issues" table renders this as the
        # "Retries" column matching the standalone mock.
        "MAX(retry_count) AS max_retry_count "
        f"FROM analytics_events{where} "
        "GROUP BY repo, issue "
        f"{order_sql} "
        "LIMIT %s"
    )
    bound_params = list(params) + [int(limit)]
    rows = _query(connect_fn, url, sql, bound_params, conn=conn)
    out: list[IssueSummaryRow] = []
    for row in rows:
        repo_v = row[0]
        issue_v = row[1]
        event_count = row[2]
        first_seen = row[3]
        last_seen = row[4]
        latest_stage = row[5]
        agent_exits = row[6]
        total_cost_usd = row[7]
        total_input_tokens = row[8]
        total_output_tokens = row[9]
        # Old fixtures may still emit 10- or 12-tuple rows; default
        # the extensions to None / 0 so tests written against the
        # prior shape continue to round-trip.
        max_review_round = row[10] if len(row) > 10 else None
        failed_agent_runs = row[11] if len(row) > 11 else 0
        max_retry_count = row[12] if len(row) > 12 else None
        out.append(
            IssueSummaryRow(
                repo=repo_v,
                issue=int(issue_v),
                event_count=int(event_count or 0),
                first_seen=first_seen,
                last_seen=last_seen,
                latest_stage=latest_stage,
                agent_exits=int(agent_exits or 0),
                total_cost_usd=(
                    float(total_cost_usd)
                    if total_cost_usd is not None
                    else None
                ),
                total_input_tokens=int(total_input_tokens or 0),
                total_output_tokens=int(total_output_tokens or 0),
                max_review_round=(
                    int(max_review_round)
                    if max_review_round is not None
                    else None
                ),
                failed_agent_runs=int(failed_agent_runs or 0),
                max_retry_count=(
                    int(max_retry_count)
                    if max_retry_count is not None
                    else None
                ),
            )
        )
    return out


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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    if events is not None and not events:
        return []
    if stages is not None and not stages:
        return []
    connect_fn = connect or _default_connect
    conditions = ["repo = %s", "issue = %s"]
    params: list[Any] = [repo, int(issue)]
    if start is not None:
        conditions.append("ts >= %s")
        params.append(start)
    if end is not None:
        conditions.append("ts < %s")
        params.append(end)
    if events:
        placeholders = ", ".join(["%s"] * len(events))
        conditions.append(f"event IN ({placeholders})")
        params.extend(events)
    if stages:
        placeholders = ", ".join(["%s"] * len(stages))
        conditions.append(f"stage IN ({placeholders})")
        params.extend(stages)
    sql = (
        "SELECT ts, event, stage, duration_s, result, "
        "agent_role, backend, exit_code, cost_usd "
        "FROM analytics_events "
        f"WHERE {' AND '.join(conditions)} "
        "ORDER BY ts ASC, id ASC"
    )
    rows = _query(connect_fn, url, sql, params, conn=conn)
    out: list[IssueEventRow] = []
    for row in rows:
        (
            ts,
            event,
            stage,
            duration_s,
            result,
            agent_role,
            backend,
            exit_code,
            cost_usd,
        ) = row
        out.append(
            IssueEventRow(
                ts=ts,
                event=event,
                stage=stage,
                duration_s=float(duration_s) if duration_s is not None else None,
                result=result,
                agent_role=agent_role,
                backend=backend,
                exit_code=int(exit_code) if exit_code is not None else None,
                cost_usd=float(cost_usd) if cost_usd is not None else None,
            )
        )
    return out
