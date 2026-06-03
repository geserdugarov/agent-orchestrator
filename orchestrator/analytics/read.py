# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Postgres read model for the `analytics_events` table.

This module is a thin, testable data-access layer over the schema
defined in `analytics-db/init/01-schema.sql` and populated by
`orchestrator.analytics.sync`. It exposes plain-Python functions for
the shapes a dashboard needs (filter dropdowns, date-bounded summary
counts, daily time-series, stage / event breakdowns, the most recent
agent-exit rows, per-issue event traces, and the chart-shaped
breakdowns the redesigned dashboard renders -- review-round buckets,
per-backend efficiency, per-repo rollups, cost-source coverage, and
the weekday x hour activity heatmap) without taking on the
Streamlit / web layer itself -- that lives in
`orchestrator/dashboard.py`.

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

`analytics_agent_runs` is a view over `event = 'agent_exit'` rows
defined in the schema; its derivations (`review_round_bucket`,
`failed`, `model`, `total_tokens`, `has_cost`) are what every
agent-run aggregate below queries against. The view has no `event`
column -- the predicate is baked in -- so functions that read from
the view honor the event filter by short-circuiting to empty when
the operator's events selection excludes `agent_exit` rather than
emitting an `event IN (...)` clause that would refer to a
non-existent column.
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
class DataExtent:
    """Earliest and latest event timestamps in the table.

    The dashboard uses this to default the sidebar date picker to a
    window that actually contains data -- a freshly-deployed database
    has no rows, so picking today's date returns nothing. Both fields
    are `None` when the table is empty or when `ANALYTICS_DB_URL` is
    unset; the dashboard branches on that to render a "no data yet"
    state.
    """

    min_ts: Optional[datetime] = None
    max_ts: Optional[datetime] = None


@dataclass(frozen=True)
class Summary:
    """Aggregate counts for a date-bounded window.

    Zero-valued by default so the "DB unset" path can return
    ``Summary()`` and the dashboard renders a still-meaningful page.
    `by_event` and `by_stage` use plain dicts because Streamlit-style
    rendering iterates them; ordering follows the SQL `GROUP BY` so
    the dashboard sees stable counts even if the rows reshuffle
    between queries. `total_agent_runs` / `failed_agent_runs` count
    `event = 'agent_exit'` rows (and the failing subset where
    `exit_code <> 0`) inside the same filtered window so the
    dashboard's "agent success rate" reads off the same query as the
    rest of the overview. `total_cache_read_tokens` /
    `total_cache_write_tokens` carry the cache-band tokens the
    redesigned dashboard's "Total tokens" KPI and sparkline include
    in the headline figure (the standalone mock's total is
    ``input + output + cache_read + cache_write``).
    """

    total_events: int = 0
    distinct_issues: int = 0
    distinct_repos: int = 0
    by_event: dict[str, int] = field(default_factory=dict)
    by_stage: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_agent_runs: int = 0
    failed_agent_runs: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    # Window-wide timeout count -- agent_exit rows whose `timed_out`
    # flag is true. Sourced from the totals query so the redesigned
    # reliability "Timeouts" tile sees every timed-out run in the
    # window, not just the latest N from `get_recent_agent_exits`.
    timed_out_agent_runs: int = 0


@dataclass(frozen=True)
class TimeSeriesPoint:
    """One (day, event, count) cell of the daily time-series.

    `day` is a `date`, not a `datetime`, because the SQL aggregates
    over `date_trunc('day', ts)` and a date matches a Plotly chart's
    axis directly. The cell carries the per-event cost / token
    aggregates as well so a "spend over time" chart can pivot off the
    same query the activity chart uses -- avoids a second round trip
    for what is already grouped by `(day, event)`. Cache-band tokens
    surface alongside input / output so the redesigned hero chart's
    `mode="type"` stack can render an Input / Output / Cache stack
    instead of dropping cache tokens on the floor. Fields default to
    zero so a fake-cursor fixture that returns just `(day, event,
    count)` rows still validates the no-aggregate path.
    """

    day: date
    event: str
    count: int
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class StageBreakdown:
    """Per-`stage` aggregate row for the stage breakdown table.

    `count` is `COUNT(*)` over every `analytics_events` row that
    carries the stage (so it includes `stage_enter` and
    `stage_evaluation` rows alongside `agent_exit`); `runs` narrows
    to the `event = 'agent_exit'` subset so the redesigned
    dashboard's "Cost by workflow stage" panel can label its
    sub-line as "runs" -- the standalone mock aggregates from
    per-agent-run records, not per-event rows.

    `avg_duration_s` is None when no row in the window had a
    non-null `duration_s` for that stage; the SQL `AVG(...)` returns
    NULL in that case rather than 0 so the dashboard can hide the
    column instead of showing a misleading zero. `total_cost_usd` /
    `total_input_tokens` / `total_output_tokens` roll up the cost /
    token figures across the stage so the breakdown table can plot
    "where the spend went". Zero-defaulted so a fake fixture without
    the run / cost / token columns still round-trips.
    """

    stage: str
    count: int
    avg_duration_s: Optional[float] = None
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    runs: int = 0


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
    events were recorded, the rolled-up cost / token totals, the
    highest review round any agent run for the issue reached, how
    many of those runs exited non-zero so the table can surface
    issues that needed multiple attempts, and the highest
    `retry_count` any agent run for the issue reached so the
    redesigned "Most expensive issues" table can carry a "Retries"
    column matching the standalone mock. Stable column order across
    the SELECT list, the dataclass, and the positional unpack in
    `get_issues` keeps the schema obvious when a future column is
    added.
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
    max_review_round: Optional[int] = None
    failed_agent_runs: int = 0
    max_retry_count: Optional[int] = None


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


@dataclass(frozen=True)
class ReviewRoundBucketRow:
    """Per-review-round count and cost of agent runs.

    `bucket` is the categorical round string
    (`0`/`1`/`2`/`3`/`4`/`5`/`6+`, plus `unknown` for NULL rounds);
    `get_review_round_breakdown` derives it from the raw
    `review_round` so rounds 3-5 stay separate and only 6+ is grouped.
    It is exposed verbatim so the dashboard chart's labels can map
    each bucket directly. `failed` is the subset of `runs`
    that exited non-zero so the chart can stack the failure ratio on
    top of the total. `total_cost_usd` rolls up the cost-priced
    agent-run rows in each bucket so the redesigned dashboard can
    plot "cost by review round" off the same query -- review rounds
    after the first one are by definition rework, and surfacing the
    cost of that rework is the lever the operator has. Rows with
    `review_round IS NULL` surface under the `"unknown"` bucket so
    they remain visible -- silently dropping them would hide
    pre-review work the operator expects to see.
    """

    bucket: str
    runs: int
    failed: int = 0
    total_cost_usd: float = 0.0


@dataclass(frozen=True)
class BackendEfficiencyRow:
    """Per-`backend` aggregate of agent runs.

    Powers the dashboard's "backend efficiency" panel: total runs,
    how many failed, the average wall-clock duration (None when no
    row in the window carried a duration), and the total cost /
    token spend. `total_cache_read_tokens` / `total_cache_write_tokens`
    surface alongside input / output so the "cost / 1M tok" tile
    can divide by the same `input + output + cache` total the rest
    of the redesigned page uses (matching the standalone mock's
    accounting). Rows whose `backend` is NULL bucket under
    `"unknown"` so the chart still surfaces them rather than
    silently dropping a category.
    """

    backend: str
    runs: int
    failed: int = 0
    avg_duration_s: Optional[float] = None
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0


@dataclass(frozen=True)
class RepoBreakdownRow:
    """Per-`repo` rollup over the filter window.

    The dashboard's "activity by repo" chart plots issue and event
    counts side-by-side; `agent_exits` and `total_cost_usd` are the
    cost-focused companions. Distinct issue counts use
    `COUNT(DISTINCT issue)` because rows are already scoped to one
    repo per bucket, so the `(repo, issue)` row-constructor used by
    `get_summary` is unnecessary here.
    """

    repo: str
    issues: int
    events: int
    agent_exits: int = 0
    total_cost_usd: float = 0.0


@dataclass(frozen=True)
class CostCoverageRow:
    """Per-`cost_source` count and token rollup of agent runs.

    Powers the dashboard's "cost attribution coverage" bar.
    `total_tokens` rolls up the per-`cost_source` token volume so
    the redesigned bar can be sized by token share -- matching the
    standalone mock, which treats coverage as "what fraction of
    token volume the parser could attribute a price to" rather than
    "what fraction of runs". A small number of high-token runs can
    dominate the cost picture, so a run-count share would
    misrepresent how exposed an operator is to pricing-table gaps.
    The `unknown-price` cohort is the maintenance signal for the
    pricing table baked into `orchestrator.usage` -- it is NEVER
    collapsed into a generic "unknown" bucket here so an operator
    can see at a glance how much volume the parser could not price.
    Rows whose `cost_source` is NULL surface under `"unknown"` so
    they remain visible (this is distinct from the `unknown-price`
    string the parser writes -- a NULL is "field absent", not
    "field present with the value 'unknown-price'").
    """

    cost_source: str
    runs: int
    total_tokens: int = 0


@dataclass(frozen=True)
class BackendDailyTokensRow:
    """One `(day, backend, total_tokens)` cell of the per-backend daily
    token series.

    Powers the redesigned dashboard's "By backend" toggle on the hero
    spend & token usage chart. Reading off `analytics_agent_runs` (a
    view over `event = 'agent_exit'` rows) means the chart never
    silently caps at the `get_recent_agent_exits` `LIMIT` -- every
    backend's tokens get counted across the full window, in lockstep
    with the cost line and KPI aggregates.
    """

    day: date
    backend: str
    total_tokens: int


@dataclass(frozen=True)
class HourlyHeatmapPoint:
    """One (weekday, hour, count, total_tokens) cell of the 7x24
    activity matrix.

    `weekday` follows Postgres `EXTRACT(DOW)` which is 0=Sunday;
    the dashboard chart re-orders to a Monday-first layout if the
    operator prefers (we expose the raw value so the chart layer
    owns the presentation choice). `hour` is the hour of day in
    the same timezone the database stores `ts` in (the orchestrator
    writes UTC). `count` is the per-cell event count; `total_tokens`
    is the matching `input + output + cache_read + cache_write`
    token volume so the redesigned dashboard's "When agents run"
    heatmap can render token intensity (matching the standalone
    mock) rather than event intensity, which would over-weight the
    cheap `stage_enter` / `stage_evaluation` cells against the
    `agent_exit` rows that actually drive spend.
    """

    weekday: int
    hour: int
    count: int
    total_tokens: int = 0


@dataclass(frozen=True)
class ThroughputDayRow:
    """One day's resolved / rejected throughput count.

    Powers the dashboard's "issues resolved per day" chart: counts
    `stage_enter` rows whose `stage` is `done` (resolved) or
    `rejected` (closed without merge), grouped by day. The two
    columns are reported side by side so the chart can stack /
    group them without a second query.
    """

    day: date
    resolved: int = 0
    rejected: int = 0


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


def _agent_event_excluded(events: Optional[Sequence[str]]) -> bool:
    """True when the active event filter excludes `agent_exit` rows.

    Functions that query `analytics_agent_runs` cannot push an
    `event IN (...)` clause down into the SQL (the view has no
    `event` column -- it filters internally to `event='agent_exit'`).
    They preserve the dashboard's event-filter contract by calling
    this helper up front and short-circuiting to an empty result:

    - ``None`` -> not excluded (no event filter at all).
    - non-empty sequence that lacks ``"agent_exit"`` -> excluded.
    - empty sequence (the cleared-multiselect signal) -> excluded.

    Keeps the agent-run aggregates in lockstep with `get_summary`
    et al. when the operator clears or narrows the events filter.
    """
    if events is None:
        return False
    if not events:
        return True
    return "agent_exit" not in events


def _build_view_window_where(
    *,
    start: Optional[datetime],
    end: Optional[datetime],
    repo: Optional[str],
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
) -> tuple[str, list[Any]]:
    """`_build_window_where` minus the ``events`` clause.

    Use against `analytics_agent_runs` queries. Callers must have
    already short-circuited on `_agent_event_excluded(events)` so
    the event-filter contract is honored before the SQL is built.
    """
    return _build_window_where(
        start=start, end=end, repo=repo,
        events=None, stages=stages, issue=issue,
    )


def get_data_extent(
    *,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
) -> DataExtent:
    """Min / max `ts` across `analytics_events`.

    The dashboard reads this once at boot to default the sidebar's
    date picker to a window that actually contains data, rather
    than to "today" against a freshly-deployed empty table. Returns
    `DataExtent()` (both fields `None`) when the DB URL is unset or
    the table is empty.
    """
    url = _resolve_db_url(db_url)
    if not url:
        return DataExtent()
    connect_fn = connect or _default_connect
    rows = _query(
        connect_fn,
        url,
        "SELECT MIN(ts) AS data_min_ts, MAX(ts) AS data_max_ts "
        "FROM analytics_events",
    )
    if not rows:
        return DataExtent()
    min_ts, max_ts = rows[0]
    return DataExtent(min_ts=min_ts, max_ts=max_ts)


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
        "COALESCE(SUM(output_tokens), 0) AS total_output_tokens, "
        # Agent-run counters: scoped to `event = 'agent_exit'` rows
        # inside the same window so the dashboard's success-rate
        # metric reads off the same query as the rest of the
        # overview. `exit_code <> 0` excludes NULL exit codes so an
        # in-flight or analytics-only row never counts as failed.
        "SUM(CASE WHEN event = 'agent_exit' THEN 1 ELSE 0 END) "
        "  AS total_agent_runs, "
        "SUM(CASE WHEN event = 'agent_exit' AND exit_code <> 0 "
        "         THEN 1 ELSE 0 END) AS failed_agent_runs, "
        # Cache-band token rollups so the redesigned KPI strip and
        # sparkline can include them in the "Total tokens" headline
        # (matching the standalone mock's
        # `input + output + cache_read + cache_write` accounting).
        "COALESCE(SUM(cache_read_tokens), 0) AS total_cache_read_tokens, "
        "COALESCE(SUM(cache_write_tokens), 0) AS total_cache_write_tokens, "
        # Window-wide timeout counter so the reliability "Timeouts"
        # tile aggregates every timed-out run, not just the latest N
        # `get_recent_agent_exits` returns. `timed_out IS NULL` (a
        # pre-flag row) never counts here -- only an explicit `true`
        # is a timeout, mirroring how `failed_agent_runs` excludes
        # NULL exit codes.
        "SUM(CASE WHEN event = 'agent_exit' AND timed_out = true "
        "         THEN 1 ELSE 0 END) AS timed_out_agent_runs "
        f"FROM analytics_events{where}"
    )
    totals_rows = _query(connect_fn, url, totals_sql, params)
    if not totals_rows:
        # Aggregates always return one row, but guard the empty case
        # so a fake cursor that returns [] never raises on the
        # positional unpack below.
        return Summary()
    row = totals_rows[0]
    total_events = row[0]
    distinct_issues = row[1]
    distinct_repos = row[2]
    total_cost_usd = row[3]
    total_input_tokens = row[4]
    total_output_tokens = row[5]
    # Fixtures that pre-date the agent-run / cache-token extensions
    # may still emit shorter tuples; default the missing columns to
    # zero so the test harness does not have to know about every
    # new SQL column in unrelated cases.
    total_agent_runs = row[6] if len(row) > 6 else 0
    failed_agent_runs = row[7] if len(row) > 7 else 0
    total_cache_read_tokens = row[8] if len(row) > 8 else 0
    total_cache_write_tokens = row[9] if len(row) > 9 else 0
    timed_out_agent_runs = row[10] if len(row) > 10 else 0

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
        total_agent_runs=int(total_agent_runs or 0),
        failed_agent_runs=int(failed_agent_runs or 0),
        total_cache_read_tokens=int(total_cache_read_tokens or 0),
        total_cache_write_tokens=int(total_cache_write_tokens or 0),
        timed_out_agent_runs=int(timed_out_agent_runs or 0),
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
    """Daily counts grouped by `event`, with rolled-up cost / tokens.

    Each point is `(day, event, count, cost_usd, input_tokens,
    output_tokens)` -- the dashboard pivots the count for the
    activity stacked-bar chart and the cost / token columns drive
    the spend-over-time and tokens-over-time charts without a second
    DB round trip. Returns an empty list when the DB URL is unset or
    no rows match.
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
        "SELECT date_trunc('day', ts)::date AS day, event, "
        "COUNT(*) AS c, "
        "COALESCE(SUM(cost_usd), 0) AS day_cost_usd, "
        "COALESCE(SUM(input_tokens), 0) AS day_input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS day_output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) AS day_cache_read_tokens, "
        "COALESCE(SUM(cache_write_tokens), 0) AS day_cache_write_tokens "
        f"FROM analytics_events{where} "
        "GROUP BY day, event "
        "ORDER BY day ASC, event ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    points: list[TimeSeriesPoint] = []
    for row in rows:
        day_value = row[0]
        event = row[1]
        count = row[2]
        cost_usd = row[3] if len(row) > 3 else 0.0
        input_tokens = row[4] if len(row) > 4 else 0
        output_tokens = row[5] if len(row) > 5 else 0
        cache_read_tokens = row[6] if len(row) > 6 else 0
        cache_write_tokens = row[7] if len(row) > 7 else 0
        if isinstance(day_value, datetime):
            day_value = day_value.date()
        points.append(
            TimeSeriesPoint(
                day=day_value,
                event=event,
                count=int(count),
                cost_usd=float(cost_usd or 0.0),
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                cache_read_tokens=int(cache_read_tokens or 0),
                cache_write_tokens=int(cache_write_tokens or 0),
            )
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
    """Per-stage counts, average handler duration, and cost rollups.

    Only counts rows whose `stage` is non-null (the partial-index
    case in the schema). Returns an empty list when the DB URL is
    unset or no row in the window carries a stage. The cost / token
    columns are summed across the stage so the breakdown can plot
    "spend per stage" without a second query.
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
        "SELECT stage, COUNT(*) AS c, AVG(duration_s) AS avg_dur, "
        "COALESCE(SUM(cost_usd), 0) AS stage_cost_usd, "
        "COALESCE(SUM(input_tokens), 0) AS stage_input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS stage_output_tokens, "
        # Agent-run subset of `count` so the redesigned dashboard's
        # "Cost by workflow stage" panel can label its sub-line as
        # "runs" -- the standalone mock aggregates per-agent-run
        # records, not per-event rows, so counting all
        # `analytics_events` rows would overstate stage activity.
        "SUM(CASE WHEN event = 'agent_exit' THEN 1 ELSE 0 END) "
        "  AS stage_agent_runs "
        f"FROM analytics_events{clause} "
        "GROUP BY stage ORDER BY c DESC, stage ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    out: list[StageBreakdown] = []
    for row in rows:
        stage = row[0]
        count = row[1]
        avg_dur = row[2]
        cost = row[3] if len(row) > 3 else 0.0
        in_tok = row[4] if len(row) > 4 else 0
        out_tok = row[5] if len(row) > 5 else 0
        runs = row[6] if len(row) > 6 else 0
        out.append(
            StageBreakdown(
                stage=stage,
                count=int(count),
                avg_duration_s=float(avg_dur) if avg_dur is not None else None,
                total_cost_usd=float(cost or 0.0),
                total_input_tokens=int(in_tok or 0),
                total_output_tokens=int(out_tok or 0),
                runs=int(runs or 0),
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
    if not url:
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
    rows = _query(connect_fn, url, sql, bound_params)
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


def get_review_round_breakdown(
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    repo: Optional[str] = None,
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
) -> list[ReviewRoundBucketRow]:
    """Per-review-round agent-run counts.

    Reads from `analytics_agent_runs` but derives the bucket from the
    raw `review_round` column rather than the view's
    `review_round_bucket`: rounds 0-5 are kept as individual buckets
    (`0`/`1`/`2`/`3`/`4`/`5`) and only 6+ is grouped, so the chart can
    show rework round-by-round instead of collapsing 3-5. Rows with
    `review_round IS NULL` surface under `"unknown"` so pre-review
    work stays visible. The `events` filter is honored by
    short-circuit: if the operator excluded `agent_exit` from the
    events multiselect (or cleared it), every agent-run aggregate
    returns empty so the dashboard's "show nothing for this
    dimension" semantics stays consistent across widgets.
    """
    url = _resolve_db_url(db_url)
    if not url:
        return []
    if _agent_event_excluded(events):
        return []
    connect_fn = connect or _default_connect
    where, params = _build_view_window_where(
        start=start, end=end, repo=repo,
        stages=stages, issue=issue,
    )
    sql = (
        "SELECT "
        # Derive the bucket from the raw `review_round` so rounds 3, 4
        # and 5 stay separate (the view's `review_round_bucket` collapses
        # them into a single `3-5`). 6+ is still grouped to bound the
        # long tail, and NULL rounds surface as `unknown`.
        "CASE "
        "WHEN review_round IS NULL THEN 'unknown' "
        "WHEN review_round <= 0 THEN '0' "
        "WHEN review_round >= 6 THEN '6+' "
        "ELSE review_round::text "
        "END AS bucket, "
        "COUNT(*) AS runs, "
        "SUM(CASE WHEN failed THEN 1 ELSE 0 END) AS failed_runs, "
        "COALESCE(SUM(cost_usd), 0) AS bucket_cost_usd "
        f"FROM analytics_agent_runs{where} "
        "GROUP BY bucket "
        "ORDER BY runs DESC, bucket ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    out: list[ReviewRoundBucketRow] = []
    for row in rows:
        bucket = row[0]
        runs = row[1]
        failed = row[2]
        # Older fixtures may still emit 3-tuple rows without the
        # cost rollup; default the cost to 0 so the test harness
        # does not have to know about the new SQL column in
        # unrelated cases.
        cost = row[3] if len(row) > 3 else 0.0
        out.append(
            ReviewRoundBucketRow(
                bucket=str(bucket),
                runs=int(runs or 0),
                failed=int(failed or 0),
                total_cost_usd=float(cost or 0.0),
            )
        )
    return out


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
) -> list[BackendEfficiencyRow]:
    """Per-`backend` aggregate of agent runs.

    Reads from `analytics_agent_runs`; the `failed` derivation is
    `exit_code <> 0` with NULLs preserved (so "no data" never reads
    as "succeeded"). Rows whose `backend` is NULL surface under
    `"unknown"`. The `events` filter is honored by short-circuit
    against `_agent_event_excluded` -- see
    `get_review_round_breakdown` for the rationale.
    """
    url = _resolve_db_url(db_url)
    if not url:
        return []
    if _agent_event_excluded(events):
        return []
    connect_fn = connect or _default_connect
    where, params = _build_view_window_where(
        start=start, end=end, repo=repo,
        stages=stages, issue=issue,
    )
    sql = (
        "SELECT "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "COUNT(*) AS runs, "
        "SUM(CASE WHEN failed THEN 1 ELSE 0 END) AS failed_runs, "
        "AVG(duration_s) AS avg_dur, "
        "COALESCE(SUM(cost_usd), 0) AS backend_cost_usd, "
        "COALESCE(SUM(input_tokens), 0) AS backend_input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS backend_output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) "
        "  AS backend_cache_read_tokens, "
        "COALESCE(SUM(cache_write_tokens), 0) "
        "  AS backend_cache_write_tokens "
        f"FROM analytics_agent_runs{where} "
        "GROUP BY backend_label "
        "ORDER BY runs DESC, backend_label ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    out: list[BackendEfficiencyRow] = []
    for row in rows:
        backend = row[0]
        runs = row[1]
        failed = row[2]
        avg_dur = row[3]
        cost = row[4]
        in_tok = row[5]
        out_tok = row[6]
        # Older fixtures may still emit 7-tuple rows without the
        # cache totals; default to zero so the test harness does
        # not have to know about the new SQL columns in unrelated
        # cases.
        cache_read = row[7] if len(row) > 7 else 0
        cache_write = row[8] if len(row) > 8 else 0
        out.append(
            BackendEfficiencyRow(
                backend=str(backend),
                runs=int(runs or 0),
                failed=int(failed or 0),
                avg_duration_s=(
                    float(avg_dur) if avg_dur is not None else None
                ),
                total_cost_usd=float(cost or 0.0),
                total_input_tokens=int(in_tok or 0),
                total_output_tokens=int(out_tok or 0),
                total_cache_read_tokens=int(cache_read or 0),
                total_cache_write_tokens=int(cache_write or 0),
            )
        )
    return out


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
) -> list[RepoBreakdownRow]:
    """Per-`repo` rollup of activity inside the filter window.

    Reads from the base table so the standard event / stage / date /
    repo / issue filter shape applies (no view short-circuit
    needed). `COUNT(DISTINCT issue)` is safe inside a GROUP BY repo
    because rows are already scoped to one repo per bucket -- the
    `(repo, issue)` row-constructor `get_summary` uses is only
    needed when issues are counted across repos.
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
        "SELECT repo, "
        "COUNT(DISTINCT issue) AS repo_issues, "
        "COUNT(*) AS repo_events, "
        "SUM(CASE WHEN event = 'agent_exit' THEN 1 ELSE 0 END) "
        "  AS repo_agent_exits, "
        "COALESCE(SUM(cost_usd), 0) AS repo_cost_usd "
        f"FROM analytics_events{where} "
        "GROUP BY repo "
        "ORDER BY repo_events DESC, repo ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    return [
        RepoBreakdownRow(
            repo=r,
            issues=int(iss or 0),
            events=int(ev or 0),
            agent_exits=int(ax or 0),
            total_cost_usd=float(cost or 0.0),
        )
        for r, iss, ev, ax, cost in rows
    ]


def get_cost_coverage(
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    repo: Optional[str] = None,
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
) -> list[CostCoverageRow]:
    """Per-`cost_source` count of agent runs.

    Reads from `analytics_agent_runs`. The `unknown-price` cohort
    is exposed verbatim -- never collapsed into a generic "unknown"
    bucket -- because it is the maintenance signal for the pricing
    table in `orchestrator.usage`: a growing slice means the table
    is missing SKUs the parser is seeing in the wild. Rows whose
    `cost_source` is NULL bucket under `"unknown"` (distinct from
    the `unknown-price` string the parser writes when the SKU is
    not priced). The `events` filter is honored by short-circuit
    against `_agent_event_excluded`.
    """
    url = _resolve_db_url(db_url)
    if not url:
        return []
    if _agent_event_excluded(events):
        return []
    connect_fn = connect or _default_connect
    where, params = _build_view_window_where(
        start=start, end=end, repo=repo,
        stages=stages, issue=issue,
    )
    sql = (
        "SELECT "
        "COALESCE(cost_source, 'unknown') AS source_label, "
        "COUNT(*) AS runs, "
        # Tokens-by-cost-source rollup so the dashboard can render
        # coverage as a token share. The view exposes the cache
        # columns; the standalone mock totals
        # `input + output + cache_read + cache_write` per row, so
        # we mirror that accounting here.
        "COALESCE(SUM("
        "  COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + "
        "  COALESCE(cache_read_tokens, 0) + "
        "  COALESCE(cache_write_tokens, 0)"
        "), 0) AS source_total_tokens "
        f"FROM analytics_agent_runs{where} "
        "GROUP BY source_label "
        "ORDER BY runs DESC, source_label ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    out: list[CostCoverageRow] = []
    for row in rows:
        source = row[0]
        runs = row[1]
        # Older fixtures may still emit 2-tuple rows; default the
        # token total to 0 so the test harness does not have to
        # know about the new SQL column in unrelated cases.
        tokens = row[2] if len(row) > 2 else 0
        out.append(
            CostCoverageRow(
                cost_source=str(source),
                runs=int(runs or 0),
                total_tokens=int(tokens or 0),
            )
        )
    return out


def get_backend_daily_tokens(
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    repo: Optional[str] = None,
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
) -> list[BackendDailyTokensRow]:
    """Per-`(day, backend)` token totals from `analytics_agent_runs`.

    Mirrors `get_time_series` shape-wise but split by `backend` rather
    than `event` and reading from the agent-runs view so token counts
    cover every agent run in the window. The redesigned dashboard
    used to derive the "By backend" stacked area from
    `get_recent_agent_exits`, which silently truncated at its
    `LIMIT`; this reader removes that cap so the stack stays in
    lockstep with the cost line and the KPI tiles. Rows whose
    `backend` is NULL surface under `"unknown"`. The `events` filter
    is honored by short-circuit against `_agent_event_excluded` --
    see `get_review_round_breakdown` for the rationale.
    """
    url = _resolve_db_url(db_url)
    if not url:
        return []
    if _agent_event_excluded(events):
        return []
    connect_fn = connect or _default_connect
    where, params = _build_view_window_where(
        start=start, end=end, repo=repo,
        stages=stages, issue=issue,
    )
    sql = (
        "SELECT "
        "date_trunc('day', ts)::date AS day, "
        "COALESCE(backend, 'unknown') AS backend_label, "
        # Token total includes cache_read / cache_write so the
        # backend stack mirrors the standalone mock's
        # `input + output + cache_read + cache_write` accounting.
        "COALESCE(SUM("
        "  COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + "
        "  COALESCE(cache_read_tokens, 0) + "
        "  COALESCE(cache_write_tokens, 0)"
        "), 0) AS day_backend_tokens "
        f"FROM analytics_agent_runs{where} "
        "GROUP BY day, backend_label "
        "ORDER BY day ASC, backend_label ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    out: list[BackendDailyTokensRow] = []
    for row in rows:
        day_value, backend, tokens = row
        if isinstance(day_value, datetime):
            day_value = day_value.date()
        out.append(
            BackendDailyTokensRow(
                day=day_value,
                backend=str(backend),
                total_tokens=int(tokens or 0),
            )
        )
    return out


def get_hourly_heatmap(
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    repo: Optional[str] = None,
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
) -> list[HourlyHeatmapPoint]:
    """7x24 weekday-by-hour activity counts from the base table.

    Honors the full event / stage / date / repo / issue filter
    shape (the chart should narrow with the rest of the dashboard).
    Cells with zero activity are elided -- the dashboard fills in
    the rest of the 7x24 grid at render time. `weekday` is the
    raw `EXTRACT(DOW FROM ts)` value (0 = Sunday) so the chart
    layer owns the Monday-first re-ordering choice.
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
        "SELECT "
        "EXTRACT(DOW FROM ts)::int AS weekday, "
        "EXTRACT(HOUR FROM ts)::int AS hour, "
        "COUNT(*) AS c, "
        # Per-cell token volume so the dashboard heatmap can render
        # token intensity instead of event count -- matching the
        # standalone mock's "Token volume by hour x weekday" panel.
        "COALESCE(SUM("
        "  COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + "
        "  COALESCE(cache_read_tokens, 0) + "
        "  COALESCE(cache_write_tokens, 0)"
        "), 0) AS cell_total_tokens "
        f"FROM analytics_events{where} "
        "GROUP BY weekday, hour "
        "ORDER BY weekday ASC, hour ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    out: list[HourlyHeatmapPoint] = []
    for row in rows:
        weekday = row[0]
        hour = row[1]
        count = row[2]
        # Older 3-tuple fixtures (no token column) round-trip with
        # zero token volume so unrelated tests keep working.
        tokens = row[3] if len(row) > 3 else 0
        out.append(
            HourlyHeatmapPoint(
                weekday=int(weekday),
                hour=int(hour),
                count=int(count or 0),
                total_tokens=int(tokens or 0),
            )
        )
    return out


# Stages a `stage_enter` event must carry to count as a terminal
# resolution -- `done` means merged / closed successfully,
# `rejected` means closed without merge. Kept private to this module
# because the throughput helper is the only consumer; if a future
# caller needs the same set, promote it to a documented constant.
_THROUGHPUT_RESOLVED_STAGES: tuple[str, ...] = ("done", "rejected")


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
    url = _resolve_db_url(db_url)
    if not url:
        return []
    if events is not None and "stage_enter" not in events:
        return []
    # Intersect the user's stage selection with the resolved /
    # rejected pair this widget is by definition about.
    if stages is None:
        active_stages = list(_THROUGHPUT_RESOLVED_STAGES)
    elif not stages:
        return []
    else:
        active_stages = [s for s in stages if s in _THROUGHPUT_RESOLVED_STAGES]
        if not active_stages:
            return []
    connect_fn = connect or _default_connect
    conditions = ["event = %s"]
    params: list[Any] = ["stage_enter"]
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
    placeholders = ", ".join(["%s"] * len(active_stages))
    conditions.append(f"stage IN ({placeholders})")
    params.extend(active_stages)
    where = " WHERE " + " AND ".join(conditions)
    sql = (
        "SELECT date_trunc('day', ts)::date AS day, "
        "SUM(CASE WHEN stage = 'done' THEN 1 ELSE 0 END) AS resolved, "
        "SUM(CASE WHEN stage = 'rejected' THEN 1 ELSE 0 END) AS rejected "
        f"FROM analytics_events{where} "
        "GROUP BY day "
        "ORDER BY day ASC"
    )
    rows = _query(connect_fn, url, sql, params)
    out: list[ThroughputDayRow] = []
    for row in rows:
        day_value, resolved, rejected = row
        if isinstance(day_value, datetime):
            day_value = day_value.date()
        out.append(
            ThroughputDayRow(
                day=day_value,
                resolved=int(resolved or 0),
                rejected=int(rejected or 0),
            )
        )
    return out
