# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Postgres read model for the `analytics_events` table.

This module is a thin, testable data-access layer over the schema
defined in `analytics-db/init/01-schema.sql` and populated by
`orchestrator.analytics.sync`. It exposes plain-Python functions for
the shapes a dashboard needs (filter dropdowns, date-bounded summary
counts, daily time-series, stage / event breakdowns, the most recent
agent-exit rows, and per-issue event traces) without taking on the
Streamlit / web layer itself -- that lives in a follow-up child.

Why a separate module from `analytics/sync.py`: the sync owns the
JSONL -> Postgres write path and its tolerance for malformed lines;
reads have a completely different error story (no rollback, no
content-hash dedup, no JSON adaptation) and a different injection
shape for tests. Keeping them apart means a dashboard never imports
ingest code and the sync never grows query helpers.

Connection settings come from `analytics.ANALYTICS_DB_URL`. There is
no hardcoded localhost fallback; reads are a no-op when the URL is
unset so a dashboard process can boot before the operator has
deployed Postgres (every function returns an empty / zero-valued
result and never raises in that mode). Connection or query failures
get wrapped in `AnalyticsReadError` so a caller has one exception
type to catch when the database is configured but unreachable /
mis-schemaed.

The psycopg import is deferred to call time inside `_default_connect`,
mirroring `analytics.sync`: tests inject a fake `connect(db_url)`
factory and never touch the real driver, and the module load path
stays driver-free for callers that only want the dataclass shapes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Optional, Sequence

from .. import analytics as _analytics

log = logging.getLogger(__name__)


class AnalyticsReadError(RuntimeError):
    """Raised when a query against the analytics DB fails.

    The original psycopg / driver exception is preserved as
    ``__cause__`` so the caller can introspect it for logging without
    the read module re-exporting psycopg's exception hierarchy.
    """


@dataclass(frozen=True)
class FilterOptions:
    """Distinct values for the dashboard filter dropdowns.

    Tuples (not lists) so the result is hashable and obviously
    immutable to callers that cache it. Empty tuples are the
    documented "DB unset" and "empty table" result -- the dashboard
    should render a disabled filter rather than crash.
    """

    repos: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    agent_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Summary:
    """Aggregate counts for a date-bounded window.

    Zero-valued by default so the "DB unset" path can return
    ``Summary()`` and the dashboard renders a still-meaningful page.
    `by_event` and `by_stage` use plain dicts because Streamlit-style
    rendering iterates them; ordering follows the SQL `GROUP BY` so
    the dashboard sees stable counts even if the rows reshuffle
    between queries.
    """

    total_events: int = 0
    distinct_issues: int = 0
    distinct_repos: int = 0
    by_event: dict[str, int] = field(default_factory=dict)
    by_stage: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


@dataclass(frozen=True)
class TimeSeriesPoint:
    """One (day, event, count) cell of the daily time-series.

    `day` is a `date`, not a `datetime`, because the SQL aggregates
    over `date_trunc('day', ts)` and a date matches Streamlit's chart
    axis directly. Callers that need an hourly axis should add a
    sibling function rather than parameterising this one -- the
    schema already covers it via the `ts` index.
    """

    day: date
    event: str
    count: int


@dataclass(frozen=True)
class StageBreakdown:
    """Per-`stage` aggregate row for the stage breakdown table.

    `avg_duration_s` is None when no row in the window had a
    non-null `duration_s` for that stage; the SQL `AVG(...)` returns
    NULL in that case rather than 0 so the dashboard can hide the
    column instead of showing a misleading zero.
    """

    stage: str
    count: int
    avg_duration_s: Optional[float] = None


@dataclass(frozen=True)
class EventBreakdown:
    """Per-`event` aggregate row for the event breakdown table."""

    event: str
    count: int


@dataclass(frozen=True)
class AgentExitRow:
    """One row of the recent-agent-exits overview table.

    Mirrors the columns the dashboard table renders -- intentionally a
    subset of the table, not every column. Adding a column should
    happen in lockstep with the SELECT list in `get_recent_agent_exits`
    so the positional unpack stays aligned.
    """

    ts: datetime
    repo: str
    issue: int
    stage: Optional[str]
    agent_role: Optional[str]
    backend: Optional[str]
    duration_s: Optional[float]
    exit_code: Optional[int]
    timed_out: Optional[bool]
    review_round: Optional[int]
    retry_count: Optional[int]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cost_usd: Optional[float]
    cost_source: Optional[str]


@dataclass(frozen=True)
class IssueSummaryRow:
    """One row of the date/repo-bounded issues overview table.

    The dashboard's "issues" view shows one row per `(repo, issue)`
    pair seen in the window with light aggregates: how many events
    fired, when the issue was first / last touched, the most recent
    non-null `stage` (useful as a "current status" column even though
    pinned GitHub state remains authoritative), how many `agent_exit`
    events were recorded, and the rolled-up cost / token totals.
    Stable column order across the SELECT list, the dataclass, and
    the positional unpack in `get_issues` keeps the schema obvious
    when a future column is added.
    """

    repo: str
    issue: int
    event_count: int
    first_seen: datetime
    last_seen: datetime
    latest_stage: Optional[str]
    agent_exits: int
    total_cost_usd: Optional[float]
    total_input_tokens: int
    total_output_tokens: int


@dataclass(frozen=True)
class IssueEventRow:
    """One row of the per-issue event trace.

    Slim: only the columns useful for the per-issue drill-down view.
    The dashboard can join back to `analytics_events` for the
    forensic columns (`source_path`, `source_line`, `extras`) if a
    debug view needs them later.
    """

    ts: datetime
    event: str
    stage: Optional[str]
    duration_s: Optional[float]
    result: Optional[str]
    agent_role: Optional[str]
    backend: Optional[str]
    exit_code: Optional[int]
    cost_usd: Optional[float]


def _default_connect(db_url: str) -> Any:
    """Lazy psycopg import so the module loads without the driver.

    `pyproject.toml` pins `psycopg[binary]`, but the dashboard's read
    path must not surface an ImportError when imported by callers
    that only consume the dataclasses (typing, tests, docs builds).
    Deferring the import to call time keeps the module load path
    driver-free, mirroring `analytics.sync._default_connect`.
    """
    try:
        import psycopg
    except ImportError as e:
        raise AnalyticsReadError(
            "psycopg is required for analytics.read; "
            "run `uv sync --locked` to install it"
        ) from e
    try:
        return psycopg.connect(db_url)
    except Exception as e:
        raise AnalyticsReadError(
            f"could not connect to analytics database: {e}"
        ) from e


def _resolve_db_url(db_url: Optional[str]) -> Optional[str]:
    if db_url is None:
        return _analytics.ANALYTICS_DB_URL
    return db_url


def _query(
    connect_fn: Callable[[str], Any],
    db_url: str,
    sql: str,
    params: Sequence[Any] = (),
) -> list[tuple]:
    """Run a single SELECT and return all rows as tuples.

    Read-only path -- no commit, no rollback. The connection is
    always closed in a `finally` so a query that raises mid-stream
    does not leak the descriptor. Any driver-level exception is
    wrapped in `AnalyticsReadError` so callers have one type to catch
    regardless of whether the failure was the connect, the execute,
    or the fetch.
    """
    try:
        conn = connect_fn(db_url)
    except AnalyticsReadError:
        raise
    except Exception as e:
        raise AnalyticsReadError(
            f"could not connect to analytics database: {e}"
        ) from e
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
        except Exception as e:
            raise AnalyticsReadError(
                f"analytics query failed: {e}"
            ) from e
    finally:
        try:
            conn.close()
        except Exception:
            log.exception("analytics.read: connection close failed")
    return list(rows or [])


def _distinct_strings(
    connect_fn: Callable[[str], Any],
    db_url: str,
    column: str,
) -> tuple[str, ...]:
    """Return the distinct non-null values of `column`, sorted ASC.

    `column` is an unquoted identifier baked into the SQL by callers
    that pass a literal (never user input), mirroring how
    `analytics.sync._build_insert_sql` interpolates known column
    names.
    """
    sql = (
        f"SELECT DISTINCT {column} FROM analytics_events "
        f"WHERE {column} IS NOT NULL "
        f"ORDER BY {column} ASC"
    )
    rows = _query(connect_fn, db_url, sql)
    return tuple(r[0] for r in rows if r and r[0] is not None)


def get_filter_options(
    *,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
) -> FilterOptions:
    """Distinct values populating the dashboard filter dropdowns.

    Returns an empty `FilterOptions` when `ANALYTICS_DB_URL` is unset
    or when the table is empty -- the dashboard renders disabled
    dropdowns rather than crashing. Failure to reach the configured
    database raises `AnalyticsReadError`.
    """
    url = _resolve_db_url(db_url)
    if not url:
        return FilterOptions()
    connect_fn = connect or _default_connect
    return FilterOptions(
        repos=_distinct_strings(connect_fn, url, "repo"),
        events=_distinct_strings(connect_fn, url, "event"),
        stages=_distinct_strings(connect_fn, url, "stage"),
        backends=_distinct_strings(connect_fn, url, "backend"),
        agent_roles=_distinct_strings(connect_fn, url, "agent_role"),
    )


def _build_window_where(
    *,
    start: Optional[datetime],
    end: Optional[datetime],
    repo: Optional[str],
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
) -> tuple[str, list[Any]]:
    """Compose the shared `WHERE` clause for window-scoped queries.

    Returns (clause, params); the clause includes a leading `WHERE`
    when at least one filter is set, or an empty string otherwise.
    Callers concatenate this directly into their SQL so the same
    filter shape (start / end / repo / events / stages / issue) is
    available across every aggregate.

    ``events`` / ``stages`` distinguish three cases on purpose:

    - ``None`` (the default) means "no filter on this column" --
      every row is eligible. This is what dashboard callers pass
      when the user has not interacted with the multiselect.
    - A non-empty sequence emits a parameterised ``IN (...)``
      clause -- the dashboard sends the user's selected subset.
    - An empty sequence emits a tautologically-false predicate
      (``FALSE``) so the query returns no rows. The dashboard
      treats a cleared multiselect as "show nothing for this
      dimension" rather than the previous "show everything"
      behavior; encoding that as SQL is what makes summary /
      time-series / breakdown / agent-run / issues counts move
      together when the operator drags a filter to empty.

    ``issue`` narrows to a single GitHub issue number. GitHub issue
    numbers are only unique within a repo, so the dashboard refuses
    to apply this filter when ``repo`` is not also set; the helper
    itself does not enforce that -- it just emits the predicate.
    """
    conditions: list[str] = []
    params: list[Any] = []
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
    if events is not None:
        if not events:
            conditions.append("FALSE")
        else:
            placeholders = ", ".join(["%s"] * len(events))
            conditions.append(f"event IN ({placeholders})")
            params.extend(events)
    if stages is not None:
        if not stages:
            conditions.append("FALSE")
        else:
            placeholders = ", ".join(["%s"] * len(stages))
            conditions.append(f"stage IN ({placeholders})")
            params.extend(stages)
    if not conditions:
        return "", params
    return " WHERE " + " AND ".join(conditions), params


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
) -> Summary:
    """Aggregate counts for a date-bounded window.

    `start` is inclusive, `end` is exclusive -- matching how callers
    typically build day-boundary windows (`[day, day + 1)`). `repo`
    filters to a single repo slug when set. `events` / `stages` /
    `issue` apply the same `_build_window_where` rules: ``None`` =
    no filter, non-empty sequence = ``IN (...)``, empty sequence =
    no rows match. Returns a zero-valued `Summary` when the DB URL
    is unset or the (post-filter) window holds no rows.
    """
    url = _resolve_db_url(db_url)
    if not url:
        return Summary()
    connect_fn = connect or _default_connect
    where, params = _build_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )

    totals_sql = (
        "SELECT "
        "COUNT(*) AS total_events, "
        # `(repo, issue)` row-constructor: GitHub issue numbers are
        # only unique within a repo, so a multi-repo window would
        # otherwise collapse `owner/a#1` and `owner/b#1` into one.
        "COUNT(DISTINCT (repo, issue)) AS distinct_issues, "
        "COUNT(DISTINCT repo) AS distinct_repos, "
        "COALESCE(SUM(cost_usd), 0) AS total_cost_usd, "
        "COALESCE(SUM(input_tokens), 0) AS total_input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS total_output_tokens "
        f"FROM analytics_events{where}"
    )
    totals_rows = _query(connect_fn, url, totals_sql, params)
    if not totals_rows:
        # Aggregates always return one row, but guard the empty case
        # so a fake cursor that returns [] never raises on the
        # positional unpack below.
        return Summary()
    (
        total_events,
        distinct_issues,
        distinct_repos,
        total_cost_usd,
        total_input_tokens,
        total_output_tokens,
    ) = totals_rows[0]

    by_event_sql = (
        "SELECT event, COUNT(*) AS c FROM analytics_events"
        f"{where} GROUP BY event ORDER BY c DESC, event ASC"
    )
    by_event_rows = _query(connect_fn, url, by_event_sql, params)
    by_event = {row[0]: int(row[1]) for row in by_event_rows}

    stage_where, stage_params = _build_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
    stage_clause = (
        f"{stage_where} AND stage IS NOT NULL"
        if stage_where
        else " WHERE stage IS NOT NULL"
    )
    by_stage_sql = (
        "SELECT stage, COUNT(*) AS c FROM analytics_events"
        f"{stage_clause} GROUP BY stage ORDER BY c DESC, stage ASC"
    )
    by_stage_rows = _query(connect_fn, url, by_stage_sql, stage_params)
    by_stage = {row[0]: int(row[1]) for row in by_stage_rows}

    return Summary(
        total_events=int(total_events or 0),
        distinct_issues=int(distinct_issues or 0),
        distinct_repos=int(distinct_repos or 0),
        by_event=by_event,
        by_stage=by_stage,
        total_cost_usd=float(total_cost_usd or 0.0),
        total_input_tokens=int(total_input_tokens or 0),
        total_output_tokens=int(total_output_tokens or 0),
    )


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
) -> list[TimeSeriesPoint]:
    """Daily counts grouped by `event`.

    Each point is `(day, event, count)` -- the dashboard pivots this
    into a stacked bar / line chart. Returns an empty list when the
    DB URL is unset or no rows match.
    """
    url = _resolve_db_url(db_url)
    if not url:
        return []
    connect_fn = connect or _default_connect
    where, params = _build_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
    sql = (
        "SELECT date_trunc('day', ts)::date AS day, event, COUNT(*) AS c "
        f"FROM analytics_events{where} "
        "GROUP BY day, event "
        "ORDER BY day ASC, event ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    points: list[TimeSeriesPoint] = []
    for row in rows:
        day_value, event, count = row
        if isinstance(day_value, datetime):
            day_value = day_value.date()
        points.append(
            TimeSeriesPoint(day=day_value, event=event, count=int(count))
        )
    return points


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
) -> list[StageBreakdown]:
    """Per-stage counts + average handler duration.

    Only counts rows whose `stage` is non-null (the partial-index
    case in the schema). Returns an empty list when the DB URL is
    unset or no row in the window carries a stage.
    """
    url = _resolve_db_url(db_url)
    if not url:
        return []
    connect_fn = connect or _default_connect
    where, params = _build_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
    clause = (
        f"{where} AND stage IS NOT NULL"
        if where
        else " WHERE stage IS NOT NULL"
    )
    sql = (
        "SELECT stage, COUNT(*) AS c, AVG(duration_s) AS avg_dur "
        f"FROM analytics_events{clause} "
        "GROUP BY stage ORDER BY c DESC, stage ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    out: list[StageBreakdown] = []
    for stage, count, avg_dur in rows:
        out.append(
            StageBreakdown(
                stage=stage,
                count=int(count),
                avg_duration_s=float(avg_dur) if avg_dur is not None else None,
            )
        )
    return out


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
) -> list[EventBreakdown]:
    """Per-event counts within the window.

    Mirrors `get_stage_breakdown`'s shape so the dashboard can render
    the two side-by-side without divergent typing.
    """
    url = _resolve_db_url(db_url)
    if not url:
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
    rows = _query(connect_fn, url, sql, params)
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
    if not url:
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
    rows = _query(connect_fn, url, sql, params)
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


def get_issues(
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    repo: Optional[str] = None,
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
    limit: int = 100,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
) -> list[IssueSummaryRow]:
    """Date / repo-bounded one-row-per-`(repo, issue)` overview.

    Powers the dashboard's "issues" table: each row aggregates the
    events seen for a single `(repo, issue)` pair inside the window
    (count, first / last activity ts, the most recent non-null stage
    as a "current status" hint, agent-exit count, and rolled-up cost
    / token totals). Sorted by `last_seen DESC` so the most recently
    active issues surface first. `limit` caps the row count for a
    bounded dashboard table; non-positive values short-circuit to
    an empty list, matching `get_recent_agent_exits`.

    `latest_stage` is computed with
    `(array_agg(stage ORDER BY ts DESC) FILTER (WHERE stage IS NOT NULL))[1]`
    -- a Postgres-native idiom that avoids a correlated subquery and
    stays correct when the most recent event for an issue does not
    carry a stage (e.g. an `agent_exit` after a `stage_evaluation`).
    """
    url = _resolve_db_url(db_url)
    if not url:
        return []
    if limit <= 0:
        return []
    connect_fn = connect or _default_connect
    where, params = _build_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
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
        "COALESCE(SUM(output_tokens), 0) AS total_output_tokens "
        f"FROM analytics_events{where} "
        "GROUP BY repo, issue "
        "ORDER BY last_seen DESC, repo ASC, issue ASC "
        "LIMIT %s"
    )
    bound_params = list(params) + [int(limit)]
    rows = _query(connect_fn, url, sql, bound_params)
    out: list[IssueSummaryRow] = []
    for row in rows:
        (
            repo_v,
            issue_v,
            event_count,
            first_seen,
            last_seen,
            latest_stage,
            agent_exits,
            total_cost_usd,
            total_input_tokens,
            total_output_tokens,
        ) = row
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
    if not url:
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
    rows = _query(connect_fn, url, sql, params)
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
