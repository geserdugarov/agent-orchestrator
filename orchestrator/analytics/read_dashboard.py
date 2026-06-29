# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard-facing aggregate readers the daily rollup cannot reconstruct.

The chart-shaped breakdowns the redesigned dashboard renders that
read `analytics_events` / `analytics_agent_runs` directly because
they need row-level detail or columns the daily rollup does not
carry: per-review-round development/review buckets (raw
`review_round`), per-`(agent_role, backend)` skill-trigger rates
(the `extras` JSONB the rollup omits), per-`cost_source` coverage,
per-`(day, backend)` token totals, and the weekday x hour activity
heatmap (hour-of-day precision the day-keyed rollup loses).

Re-exported unchanged through `orchestrator.analytics.read`; see
that module's docstring for the connection / URL / error contract
and the agent-run event-filter short-circuit these helpers share.
Raw-table overview readers live in `read_raw`; the rollup-backed
aggregates in `read_rollup`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from .connection import _default_connect
from .db_url import _resolve_db_url
from .predicates import (
    _agent_event_excluded,
    _build_view_window_where,
    _build_window_where,
)
from .query import _query
from .read_models import (
    BackendDailyTokensRow,
    CostCoverageRow,
    HourlyHeatmapPoint,
    ReviewRoundBucketRow,
    SkillTriggerRateRow,
)


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
    conn: Any = None,
) -> list[ReviewRoundBucketRow]:
    """Per-review-round development/review agent-run counts.

    Reads from `analytics_agent_runs` but derives the bucket from the
    raw `review_round` column rather than the view's
    `review_round_bucket`: rounds 0-5 are kept as individual buckets
    (`0`/`1`/`2`/`3`/`4`/`5`) and only 6+ is grouped, so the chart can
    show rework round-by-round instead of collapsing 3-5. Only
    `developer` and `reviewer` agent roles feed this panel; decomposer
    and question runs are lifecycle costs, not review-cycle costs.
    Rows with `review_round IS NULL` surface under `"unknown"` if
    they are still development/review runs. Historical implementing
    rows that predate fresh-spawn `review_round=0` logging are
    bucketed as `0`. The `events` filter is honored by
    short-circuit: if the operator excluded `agent_exit` from the
    events multiselect (or cleared it), every agent-run aggregate
    returns empty so the dashboard's "show nothing for this
    dimension" semantics stays consistent across widgets.
    """
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    if _agent_event_excluded(events):
        return []
    connect_fn = connect or _default_connect
    where, params = _build_view_window_where(
        start=start, end=end, repo=repo,
        stages=stages, issue=issue,
    )
    role_clause = "agent_role IN ('developer', 'reviewer')"
    if where:
        where = f"{where} AND {role_clause}"
    else:
        where = f" WHERE {role_clause}"
    # Each run is split into a cache portion (the share of its tokens
    # billed as cached / cache-read / cache-write) and a no-cache
    # portion (the remaining input + output tokens). Cost is attributed
    # proportionally so the per-round chart shows what fraction of
    # spend actually flowed through the cache vs ran against fresh
    # tokens -- the prior binary "any cache token => fully cache"
    # classification collapsed to ~100% cache once every backend
    # started reporting cache writes on the first call, leaving the
    # no-cache stack empty. `total_cache_tokens` / `total_tokens` would
    # let us inline these from the view but the columns only live
    # there, not on the raw table, so encode the expressions directly
    # off the underlying token columns for forward-compat with rollup
    # paths.
    #
    # `cached_tokens` (Codex) is a subset of `input_tokens` -- the
    # portion of the prompt served from cache -- so it stays out of
    # the denominator to avoid double-counting. `cache_read_tokens` /
    # `cache_write_tokens` (Claude) are reported alongside `input_tokens`
    # rather than inside it, so they add to the denominator normally.
    cache_tokens_expr = (
        "(COALESCE(cached_tokens, 0) "
        "+ COALESCE(cache_read_tokens, 0) "
        "+ COALESCE(cache_write_tokens, 0))"
    )
    all_tokens_expr = (
        "(COALESCE(input_tokens, 0) "
        "+ COALESCE(output_tokens, 0) "
        "+ COALESCE(cache_read_tokens, 0) "
        "+ COALESCE(cache_write_tokens, 0))"
    )
    # Guard the denominator so a token-less row contributes its whole
    # cost (if any) to the no-cache stack rather than dividing by zero.
    cache_fraction_expr = (
        f"CASE WHEN {all_tokens_expr} = 0 THEN 0 "
        f"ELSE {cache_tokens_expr}::numeric / {all_tokens_expr}::numeric "
        f"END"
    )
    sql = (
        "SELECT "
        # Derive the bucket from the raw `review_round` so rounds 3, 4
        # and 5 stay separate (the view's `review_round_bucket` collapses
        # them into a single `3-5`). 6+ is still grouped to bound the
        # long tail. Fresh implementing runs now log review_round=0;
        # this explicit stage/role fallback keeps older rows in the
        # same first-pass development bucket.
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
        f"THEN COALESCE(cost_usd, 0) * ({cache_fraction_expr}) "
        "ELSE 0 END), 0) AS developer_cache_cost_usd, "
        "COALESCE(SUM(CASE WHEN agent_role = 'developer' "
        f"THEN COALESCE(cost_usd, 0) * (1 - ({cache_fraction_expr})) "
        "ELSE 0 END), 0) AS developer_no_cache_cost_usd, "
        "COALESCE(SUM(CASE WHEN agent_role = 'reviewer' "
        f"THEN COALESCE(cost_usd, 0) * ({cache_fraction_expr}) "
        "ELSE 0 END), 0) AS reviewer_cache_cost_usd, "
        "COALESCE(SUM(CASE WHEN agent_role = 'reviewer' "
        f"THEN COALESCE(cost_usd, 0) * (1 - ({cache_fraction_expr})) "
        "ELSE 0 END), 0) AS reviewer_no_cache_cost_usd "
        f"FROM analytics_agent_runs{where} "
        "GROUP BY bucket "
        "ORDER BY runs DESC, bucket ASC"
    )
    rows = _query(connect_fn, url, sql, params, conn=conn)
    out: list[ReviewRoundBucketRow] = []
    for row in rows:
        bucket = row[0]
        runs = row[1]
        failed = row[2]
        # Older fixtures may still emit rows without the role / cache
        # split; default those columns so unrelated tests keep
        # round-tripping.
        cost = row[3] if len(row) > 3 else 0.0
        developer_runs = row[4] if len(row) > 4 else 0
        reviewer_runs = row[5] if len(row) > 5 else 0
        developer_cost = row[6] if len(row) > 6 else 0.0
        reviewer_cost = row[7] if len(row) > 7 else 0.0
        developer_cache_cost = row[8] if len(row) > 8 else 0.0
        developer_no_cache_cost = row[9] if len(row) > 9 else 0.0
        reviewer_cache_cost = row[10] if len(row) > 10 else 0.0
        reviewer_no_cache_cost = row[11] if len(row) > 11 else 0.0
        out.append(
            ReviewRoundBucketRow(
                bucket=str(bucket),
                runs=int(runs or 0),
                failed=int(failed or 0),
                total_cost_usd=float(cost or 0.0),
                developer_runs=int(developer_runs or 0),
                reviewer_runs=int(reviewer_runs or 0),
                developer_cost_usd=float(developer_cost or 0.0),
                reviewer_cost_usd=float(reviewer_cost or 0.0),
                developer_cache_cost_usd=float(developer_cache_cost or 0.0),
                developer_no_cache_cost_usd=float(
                    developer_no_cache_cost or 0.0
                ),
                reviewer_cache_cost_usd=float(reviewer_cache_cost or 0.0),
                reviewer_no_cache_cost_usd=float(
                    reviewer_no_cache_cost or 0.0
                ),
            )
        )
    return out


def get_skill_trigger_rates(
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
) -> list[SkillTriggerRateRow]:
    """Per-`(agent_role, backend)` skill-trigger rates over agent runs.

    Reads the base `analytics_events` table rather than the rollup: the
    skill fields live in `extras` JSONB, which the materialized rollup
    does not carry, so this widget stays a pure read-side addition with
    zero DDL. Pins `event = 'agent_exit'` so only tracked agent runs
    count, and short-circuits to empty when the events multiselect
    excludes `agent_exit` (the same contract `get_backend_efficiency`
    honors). A run counts toward `skill_runs` when its `extras` carries
    a `skills_triggered` key -- `record_agent_exit` writes that key only
    when `TRACK_SKILL_TRIGGERS` is on *and* a skill fired, so its
    presence is the firm "a skill triggered" signal. `total_triggers`
    sums `skills_triggered_count`. NULL `agent_role` / `backend` bucket
    under `"unknown"`. Rows are ordered skill-active groups first.
    """
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    if _agent_event_excluded(events):
        return []
    connect_fn = connect or _default_connect
    where, params = _build_window_where(
        start=start, end=end, repo=repo,
        events=None, stages=stages, issue=issue,
    )
    clause = (
        f"{where} AND event = 'agent_exit'"
        if where
        else " WHERE event = 'agent_exit'"
    )
    # `extras -> 'skills_triggered' IS NOT NULL` (not the jsonb `?`
    # operator) tests key presence without tripping the `?`/`%s`
    # placeholder ambiguity some drivers and poolers apply.
    sql = (
        "SELECT "
        "COALESCE(agent_role, 'unknown') AS role_label, "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "COUNT(*) AS runs, "
        "COUNT(*) FILTER "
        "  (WHERE extras -> 'skills_triggered' IS NOT NULL) AS skill_runs, "
        "COALESCE(SUM((extras ->> 'skills_triggered_count')::int), 0) "
        "  AS total_triggers "
        f"FROM analytics_events{clause} "
        "GROUP BY role_label, backend_label "
        "ORDER BY skill_runs DESC, runs DESC, role_label ASC, "
        "backend_label ASC"
    )
    rows = _query(connect_fn, url, sql, params, conn=conn)
    out: list[SkillTriggerRateRow] = []
    for row in rows:
        role = row[0]
        backend = row[1]
        runs = row[2]
        # Older fixtures may emit 3-tuple rows without the skill
        # columns; default to zero so unrelated test cases need not
        # know about the JSONB aggregates.
        skill_runs = row[3] if len(row) > 3 else 0
        total_triggers = row[4] if len(row) > 4 else 0
        out.append(
            SkillTriggerRateRow(
                agent_role=str(role) if role is not None else "unknown",
                backend=str(backend) if backend is not None else "unknown",
                runs=int(runs or 0),
                skill_runs=int(skill_runs or 0),
                total_triggers=int(total_triggers or 0),
            )
        )
    return out


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
    conn: Any = None,
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
    if conn is None and not url:
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
    rows = _query(connect_fn, url, sql, params, conn=conn)
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
    conn: Any = None,
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
    if conn is None and not url:
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
    rows = _query(connect_fn, url, sql, params, conn=conn)
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
    tz_offset_hours: int = 0,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
    conn: Any = None,
) -> list[HourlyHeatmapPoint]:
    """7x24 weekday-by-hour activity counts from the base table.

    Honors the full event / stage / date / repo / issue filter
    shape (the chart should narrow with the rest of the dashboard).
    Cells with zero activity are elided -- the dashboard fills in
    the rest of the 7x24 grid at render time. `weekday` is the
    raw `EXTRACT(DOW FROM ts)` value (0 = Sunday) so the chart
    layer owns the Monday-first re-ordering choice.

    `tz_offset_hours` shifts `ts` by the given integer hours before
    the `EXTRACT(DOW / HOUR ...)` calls so the operator can view
    the heatmap in a non-UTC timezone (the orchestrator stores
    `ts` in UTC). Zero is the historical behavior.
    """
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    connect_fn = connect or _default_connect
    where, params = _build_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
    # Normalize `ts` (TIMESTAMPTZ) to a UTC naive `TIMESTAMP` via
    # `AT TIME ZONE 'UTC'` before applying the offset and extracting.
    # `EXTRACT()` on a TIMESTAMPTZ is read in the database session
    # timezone, so without this normalization a non-UTC session would
    # shift the buckets again on top of our explicit offset.
    # Parameterised so the integer is never spliced into the SQL.
    sql = (
        "SELECT "
        "EXTRACT(DOW FROM ((ts AT TIME ZONE 'UTC') "
        "+ %s * INTERVAL '1 hour'))::int AS weekday, "
        "EXTRACT(HOUR FROM ((ts AT TIME ZONE 'UTC') "
        "+ %s * INTERVAL '1 hour'))::int AS hour, "
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
    offset_int = int(tz_offset_hours)
    params = [offset_int, offset_int, *params]
    rows = _query(connect_fn, url, sql, params, conn=conn)
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
