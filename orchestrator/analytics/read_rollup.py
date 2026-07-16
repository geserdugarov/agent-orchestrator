# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Rollup-backed analytics readers over `analytics_daily_rollup`.

The window-bounded aggregate readers whose shapes the daily rollup
materialised view can reconstruct exactly -- summary counts, the
KPI previous-window scalars, the daily time-series, the per-stage
breakdown, per-backend efficiency, the per-repo rollup, and the
resolved / rejected throughput counts. Each rollup row already
aggregates `(day, repo, issue, event, stage, backend, cost_source)`
events, so reading from it collapses the events-table scan to a
tiny day-keyed scan once the events table grows.

Re-exported unchanged through `orchestrator.analytics.read`; see
that module's docstring for the connection / URL / error contract
and for why each shape is rollup-backed rather than reading the
base table. Raw-table overview readers live in `read_raw`; the
remaining view-backed dashboard chart breakdowns in
`read_dashboard`.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from orchestrator.analytics.predicates import (
    _DAILY_ROLLUP_VIEW,
    _WindowFilters,
    _agent_event_excluded,
    _append_where_condition,
    _build_rollup_window_where,
    _prepend_where_condition,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import (
    BackendEfficiencyRow,
    RepoBreakdownRow,
    StageBreakdown,
    Summary,
    ThroughputDayRow,
    TimeSeriesPoint,
)


_SUMMARY_TOTAL_FIELD_CASTS = (
    ("total_events", int),
    ("distinct_issues", int),
    ("distinct_repos", int),
    ("total_cost_usd", float),
    ("total_input_tokens", int),
    ("total_output_tokens", int),
    ("total_agent_runs", int),
    ("failed_agent_runs", int),
    ("total_cache_read_tokens", int),
    ("total_cache_write_tokens", int),
    ("timed_out_agent_runs", int),
)


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


def _summary_totals_row(rows: Sequence[tuple]) -> Optional[tuple]:
    """Return the totals row emitted by the combined query, if present."""
    totals_row: Optional[tuple] = None
    for row in rows:
        if row and row[0] == "t":
            totals_row = row
    return totals_row


def _ordered_summary_counts(
    rows: Sequence[tuple],
    row_kind: str,
) -> dict[str, int]:
    """Convert one breakdown row kind to count-descending order."""
    counts = [
        (row[1], int(row[2] or 0))
        for row in rows
        if row and row[0] == row_kind and row[1] is not None
    ]
    counts.sort(key=_summary_count_order)
    return dict(counts)


def _summary_count_order(pair: tuple[str, int]) -> tuple[int, str]:
    return -pair[1], pair[0]


def _summary_total_values(totals_row: tuple) -> dict[str, Any]:
    """Map the totals columns to typed Summary field values."""
    return {
        field_name: field_cast(raw_value or 0)
        for (field_name, field_cast), raw_value in zip(
            _SUMMARY_TOTAL_FIELD_CASTS,
            totals_row[2:],
        )
    }


def _summary_from_rows(rows: Sequence[tuple]) -> Summary:
    """Convert combined-query rows into the public Summary model."""
    by_event = _ordered_summary_counts(rows, "e")
    by_stage = _ordered_summary_counts(rows, "s")
    totals_row = _summary_totals_row(rows)
    if totals_row is None:
        return Summary(by_event=by_event, by_stage=by_stage)
    return Summary(
        by_event=by_event,
        by_stage=by_stage,
        **_summary_total_values(totals_row),
    )


def _row_value(row: Sequence[Any], index: int, default: Any = 0) -> Any:
    if len(row) <= index:
        return default
    return row[index]


def _day_value(day: Any) -> Any:
    if isinstance(day, datetime):
        return day.date()
    return day


def _float_or_none(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    return float(raw)


def get_summary(
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
) -> Summary:
    """Aggregate counts for a date-bounded window.

    `start` is inclusive, `end` is exclusive -- matching how callers
    typically build day-boundary windows (`[day, day + 1)`). `repo`
    filters to a single repo slug when set. `events` / `stages` /
    `issue` apply the same rollup-window rules: ``None`` =
    no filter, non-empty sequence = ``IN (...)``, empty sequence =
    no rows match. Returns a zero-valued `Summary` when the DB URL
    is unset or the (post-filter) window holds no rows.
    """
    query = _ReadQuery.resolve(db_url, connect, conn)
    if not query.available:
        return Summary()
    filters = _WindowFilters(
        start=start,
        end=end,
        repo=repo,
        events=events,
        stages=stages,
        issue=issue,
    )
    return _summary_from_rows(_query_summary_rows(query, filters))


def get_kpi_prev(
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
) -> Summary:
    """Previous-window scalars for the dashboard's KPI delta pills.

    A trimmed `get_summary` that only computes the cost / token /
    agent-run totals the dashboard reads off `prev_summary` -- the
    KPI strip's delta indicators (`total_cost_usd`, the
    `input + output + cache_read + cache_write` token sum,
    `total_agent_runs`) and `compute_insights`'s cost-trend banner
    (`total_cost_usd`). The full `Summary` shape's per-event /
    per-stage breakdowns, distinct-issue / distinct-repo counts, and
    failure / timeout counters are not consumed in the
    previous-window path, so this reader skips the
    `COUNT(DISTINCT)`s and the `GROUP BY` follow-ups entirely. The
    return value is still a `Summary` so existing call sites
    (`compute_insights(..., prev_summary=...)`) keep their shape;
    the unread fields stay at their dataclass defaults.

    Returns `Summary()` when `ANALYTICS_DB_URL` is unset (mirroring
    `get_summary`). Filter semantics for `start` / `end` / `repo` /
    `events` / `stages` / `issue` are identical to `get_summary` --
    they share `_build_window_where`.
    """
    query = _ReadQuery.resolve(db_url, connect, conn)
    if not query.available:
        return Summary()
    filters = _WindowFilters(
        start=start,
        end=end,
        repo=repo,
        events=events,
        stages=stages,
        issue=issue,
    )
    return _kpi_prev_summary(query, filters)


def _kpi_prev_sql(where: str) -> str:
    return (
        "SELECT "
        "COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd, "
        "COALESCE(SUM(total_input_tokens), 0) AS total_input_tokens, "
        "COALESCE(SUM(total_output_tokens), 0) AS total_output_tokens, "
        "COALESCE(SUM(total_cache_read_tokens), 0) "
        "  AS total_cache_read_tokens, "
        "COALESCE(SUM(total_cache_write_tokens), 0) "
        "  AS total_cache_write_tokens, "
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN event_count ELSE 0 END), 0) "
        "  AS total_agent_runs "
        f"FROM {_DAILY_ROLLUP_VIEW}{where}"
    )


def _kpi_prev_summary(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> Summary:
    where, bindings = _build_rollup_window_where(filters)
    rows = query.select(_kpi_prev_sql(where), bindings)
    if not rows:
        return Summary()
    row = rows[0]
    return Summary(
        total_cost_usd=float(row[0] or 0.0),
        total_input_tokens=int(row[1] or 0),
        total_output_tokens=int(row[2] or 0),
        total_cache_read_tokens=int(row[3] or 0),
        total_cache_write_tokens=int(row[4] or 0),
        total_agent_runs=int(_row_value(row, 5) or 0),
    )


def _time_series_from_row(row: Sequence[Any]) -> TimeSeriesPoint:
    return TimeSeriesPoint(
        day=_day_value(row[0]),
        event=row[1],
        count=int(row[2]),
        cost_usd=float(_row_value(row, 3, 0.0) or 0.0),
        input_tokens=int(_row_value(row, 4) or 0),
        output_tokens=int(_row_value(row, 5) or 0),
        cache_read_tokens=int(_row_value(row, 6) or 0),
        cache_write_tokens=int(_row_value(row, 7) or 0),
    )


def _time_series_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[TimeSeriesPoint]:
    where, bindings = _build_rollup_window_where(filters)
    rows = query.select(
        "SELECT day, event, "
        "COALESCE(SUM(event_count), 0) AS c, "
        "COALESCE(SUM(total_cost_usd), 0) AS day_cost_usd, "
        "COALESCE(SUM(total_input_tokens), 0) AS day_input_tokens, "
        "COALESCE(SUM(total_output_tokens), 0) AS day_output_tokens, "
        "COALESCE(SUM(total_cache_read_tokens), 0) "
        "  AS day_cache_read_tokens, "
        "COALESCE(SUM(total_cache_write_tokens), 0) "
        "  AS day_cache_write_tokens "
        f"FROM {_DAILY_ROLLUP_VIEW}{where} "
        "GROUP BY day, event "
        "ORDER BY day ASC, event ASC",
        bindings,
    )
    return [_time_series_from_row(row) for row in rows]


def get_time_series(
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
) -> list[TimeSeriesPoint]:
    """Daily counts grouped by `event`, with rolled-up cost / tokens.

    Each point is `(day, event, count, cost_usd, input_tokens,
    output_tokens)` -- the dashboard pivots the count for the
    activity stacked-bar chart and the cost / token columns drive
    the spend-over-time and tokens-over-time charts without a second
    DB round trip. Returns an empty list when the DB URL is unset or
    no rows match.
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
    return _time_series_rows(query, filters)


_ROLLUP_CACHE_TOKENS_SQL = (
    "(COALESCE(total_cached_tokens, 0) "
    "+ COALESCE(total_cache_read_tokens, 0) "
    "+ COALESCE(total_cache_write_tokens, 0))"
)
_ROLLUP_ALL_TOKENS_SQL = (
    "(COALESCE(total_input_tokens, 0) "
    "+ COALESCE(total_output_tokens, 0) "
    "+ COALESCE(total_cache_read_tokens, 0) "
    "+ COALESCE(total_cache_write_tokens, 0))"
)
_ROLLUP_CACHE_FRACTION_SQL = (
    f"CASE WHEN {_ROLLUP_ALL_TOKENS_SQL} = 0 THEN 0 "
    f"ELSE {_ROLLUP_CACHE_TOKENS_SQL}::numeric "
    f"/ {_ROLLUP_ALL_TOKENS_SQL}::numeric END"
)


def _stage_breakdown_sql(clause: str) -> str:
    return (
        "SELECT stage, "
        "COALESCE(SUM(event_count), 0) AS c, "
        "SUM(duration_s_sum) / NULLIF(SUM(duration_s_count), 0) "
        "  AS avg_dur, "
        "COALESCE(SUM(total_cost_usd), 0) AS stage_cost_usd, "
        "COALESCE(SUM(total_input_tokens), 0) AS stage_input_tokens, "
        "COALESCE(SUM(total_output_tokens), 0) AS stage_output_tokens, "
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN event_count ELSE 0 END), 0) "
        "  AS stage_agent_runs, "
        "COALESCE(SUM(COALESCE(total_cost_usd, 0) "
        f"* ({_ROLLUP_CACHE_FRACTION_SQL})), 0) AS stage_cache_cost_usd, "
        "COALESCE(SUM(COALESCE(total_cost_usd, 0) "
        f"* (1 - ({_ROLLUP_CACHE_FRACTION_SQL}))), 0) "
        "AS stage_no_cache_cost_usd "
        f"FROM {_DAILY_ROLLUP_VIEW}{clause} "
        "GROUP BY stage ORDER BY c DESC, stage ASC"
    )


def _stage_breakdown_from_row(row: Sequence[Any]) -> StageBreakdown:
    return StageBreakdown(
        stage=row[0],
        count=int(row[1]),
        avg_duration_s=_float_or_none(row[2]),
        total_cost_usd=float(_row_value(row, 3, 0.0) or 0.0),
        total_input_tokens=int(_row_value(row, 4) or 0),
        total_output_tokens=int(_row_value(row, 5) or 0),
        runs=int(_row_value(row, 6) or 0),
        cache_cost_usd=float(_row_value(row, 7, 0.0) or 0.0),
        no_cache_cost_usd=float(_row_value(row, 8, 0.0) or 0.0),
    )


def _stage_breakdown_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[StageBreakdown]:
    where, bindings = _build_rollup_window_where(filters)
    clause = _append_where_condition(where, "stage IS NOT NULL")
    rows = query.select(_stage_breakdown_sql(clause), bindings)
    return [_stage_breakdown_from_row(row) for row in rows]


def get_stage_breakdown(
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
) -> list[StageBreakdown]:
    """Per-stage counts, average handler duration, and cost rollups.

    Only counts rows whose `stage` is non-null (the partial-index
    case in the schema). Returns an empty list when the DB URL is
    unset or no row in the window carries a stage. The cost / token
    columns are summed across the stage so the breakdown can plot
    "spend per stage" without a second query.
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
    return _stage_breakdown_rows(query, filters)


def _backend_efficiency_sql(clause: str) -> str:
    return (
        "SELECT "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "COALESCE(SUM(event_count), 0) AS runs, "
        "COALESCE(SUM(failed_count), 0) AS failed_runs, "
        "SUM(duration_s_sum) / NULLIF(SUM(duration_s_count), 0) "
        "  AS avg_dur, "
        "COALESCE(SUM(total_cost_usd), 0) AS backend_cost_usd, "
        "COALESCE(SUM(total_input_tokens), 0) AS backend_input_tokens, "
        "COALESCE(SUM(total_output_tokens), 0) AS backend_output_tokens, "
        "COALESCE(SUM(total_cache_read_tokens), 0) "
        "  AS backend_cache_read_tokens, "
        "COALESCE(SUM(total_cache_write_tokens), 0) "
        "  AS backend_cache_write_tokens "
        f"FROM {_DAILY_ROLLUP_VIEW}{clause} "
        "GROUP BY backend_label "
        "ORDER BY runs DESC, backend_label ASC"
    )


def _backend_efficiency_from_row(
    row: Sequence[Any],
) -> BackendEfficiencyRow:
    return BackendEfficiencyRow(
        backend=str(row[0]),
        runs=int(row[1] or 0),
        failed=int(row[2] or 0),
        avg_duration_s=_float_or_none(row[3]),
        total_cost_usd=float(row[4] or 0.0),
        total_input_tokens=int(row[5] or 0),
        total_output_tokens=int(row[6] or 0),
        total_cache_read_tokens=int(_row_value(row, 7) or 0),
        total_cache_write_tokens=int(_row_value(row, 8) or 0),
    )


def _backend_efficiency_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[BackendEfficiencyRow]:
    where, bindings = _build_rollup_window_where(filters.without_events())
    clause = _append_where_condition(where, "event = 'agent_exit'")
    rows = query.select(_backend_efficiency_sql(clause), bindings)
    return [_backend_efficiency_from_row(row) for row in rows]


def get_backend_efficiency(
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
) -> list[BackendEfficiencyRow]:
    """Per-`backend` aggregate of agent runs.

    Reads from `analytics_daily_rollup` with `event = 'agent_exit'`
    pinned in the WHERE clause so the aggregate matches the previous
    `analytics_agent_runs`-backed query (the view filters internally
    to `event = 'agent_exit'`). The rollup carries `failed_count`
    pre-derived (`exit_code IS NOT NULL AND exit_code <> 0`) so the
    NULL-exit-code rows that the previous SQL excluded are excluded
    here too. Rows whose `backend` is NULL surface under `"unknown"`.
    The `events` filter is honored by short-circuit against
    `_agent_event_excluded` -- see `get_review_round_breakdown` for
    the rationale. `AVG(duration_s)` is recovered from the rollup as
    `SUM(duration_s_sum) / SUM(duration_s_count)` so averaging
    averages across days never blurs the row-weighted mean.
    """
    query = _ReadQuery.resolve(db_url, connect, conn)
    if not query.available:
        return []
    if _agent_event_excluded(events):
        return []
    filters = _WindowFilters(
        start=start,
        end=end,
        repo=repo,
        stages=stages,
        issue=issue,
    )
    return _backend_efficiency_rows(query, filters)


def _repo_breakdown_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[RepoBreakdownRow]:
    where, bindings = _build_rollup_window_where(filters)
    rows = query.select(
        "SELECT repo, "
        "COUNT(DISTINCT issue) AS repo_issues, "
        "COALESCE(SUM(event_count), 0) AS repo_events, "
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN event_count ELSE 0 END), 0) "
        "  AS repo_agent_exits, "
        "COALESCE(SUM(total_cost_usd), 0) AS repo_cost_usd "
        f"FROM {_DAILY_ROLLUP_VIEW}{where} "
        "GROUP BY repo "
        "ORDER BY repo_events DESC, repo ASC",
        bindings,
    )
    return [
        RepoBreakdownRow(
            repo=row[0],
            issues=int(row[1] or 0),
            events=int(row[2] or 0),
            agent_exits=int(row[3] or 0),
            total_cost_usd=float(row[4] or 0.0),
        )
        for row in rows
    ]


def get_repo_breakdown(
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
) -> list[RepoBreakdownRow]:
    """Per-`repo` rollup of activity inside the filter window.

    Reads from `analytics_daily_rollup` so the standard event /
    stage / date / repo / issue filter shape still applies (the
    rollup carries an `event` column even though the agent-run view
    does not, so no Python-side short-circuit is needed). The
    rollup is keyed on `(day, repo, issue, ...)`, so
    `COUNT(DISTINCT issue)` per `GROUP BY repo` is still exact --
    each rollup row carries one issue, so distinct counting after
    `GROUP BY repo` does not over-count.
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
    return _repo_breakdown_rows(query, filters)


# Stages a `stage_enter` event must carry to count as a terminal
# resolution -- `done` means merged / closed successfully,
# `rejected` means closed without merge. Kept private to this module
# because the throughput helper is the only consumer; if a future
# caller needs the same set, promote it to a documented constant.
_THROUGHPUT_RESOLVED_STAGES: tuple[str, ...] = ("done", "rejected")


def _selected_throughput_stages(
    stages: Optional[Sequence[str]],
) -> tuple[str, ...]:
    if stages is None:
        return _THROUGHPUT_RESOLVED_STAGES
    return tuple(
        stage for stage in stages if stage in _THROUGHPUT_RESOLVED_STAGES
    )


def _throughput_from_row(row: Sequence[Any]) -> ThroughputDayRow:
    return ThroughputDayRow(
        day=_day_value(row[0]),
        resolved=int(row[1] or 0),
        rejected=int(row[2] or 0),
    )


def _throughput_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[ThroughputDayRow]:
    if filters.events is not None and "stage_enter" not in filters.events:
        return []
    active_stages = _selected_throughput_stages(filters.stages)
    if not active_stages:
        return []
    scoped_filters = replace(filters, events=None, stages=active_stages)
    where, bindings = _build_rollup_window_where(scoped_filters)
    where = _prepend_where_condition(where, "event = %s")
    bindings.insert(0, "stage_enter")
    rows = query.select(
        "SELECT day, "
        "COALESCE(SUM(CASE WHEN stage = 'done' "
        "                  THEN event_count ELSE 0 END), 0) AS resolved, "
        "COALESCE(SUM(CASE WHEN stage = 'rejected' "
        "                  THEN event_count ELSE 0 END), 0) AS rejected "
        f"FROM {_DAILY_ROLLUP_VIEW}{where} "
        "GROUP BY day "
        "ORDER BY day ASC",
        bindings,
    )
    return [_throughput_from_row(row) for row in rows]


def get_throughput_breakdown(
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
) -> list[ThroughputDayRow]:
    """Daily resolved / rejected `stage_enter` counts.

    Counts `event = 'stage_enter'` rows whose `stage` is `done`
    (resolved) or `rejected`, grouped by day. The widget answers
    "how many issues completed per day" and is distinct from the
    activity throughput plotted by `get_time_series` (which counts
    every event).

    Honors the operator's filters:

    - `events` short-circuits to empty when the multiselect
      excludes `stage_enter` (or is cleared), matching how
      `get_recent_agent_exits` honors `agent_exit`.
    - `stages` short-circuits when the multiselect excludes both
      `done` and `rejected`, or is cleared; otherwise the
      intersection is what narrows the SQL.
    - `start` / `end` / `repo` / `issue` apply as in every other
      reader.
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
    return _throughput_rows(query, filters)
