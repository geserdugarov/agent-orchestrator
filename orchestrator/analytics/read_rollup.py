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

from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from .connection import _default_connect
from .db_url import _resolve_db_url
from .predicates import (
    _DAILY_ROLLUP_VIEW,
    _agent_event_excluded,
    _build_rollup_window_where,
)
from .query import _query
from .read_models import (
    BackendEfficiencyRow,
    RepoBreakdownRow,
    StageBreakdown,
    Summary,
    ThroughputDayRow,
    TimeSeriesPoint,
)


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
    `issue` apply the same `_build_window_where` rules: ``None`` =
    no filter, non-empty sequence = ``IN (...)``, empty sequence =
    no rows match. Returns a zero-valued `Summary` when the DB URL
    is unset or the (post-filter) window holds no rows.
    """
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return Summary()
    connect_fn = connect or _default_connect
    where, params = _build_rollup_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )

    # One round-trip against the rollup materialised view. Each
    # rollup row already aggregates `(day, repo, issue, event,
    # stage, backend, cost_source)`-keyed events from the base
    # table, so `SUM(event_count)` recovers `COUNT(*)`, and the
    # token / cost / failure / timeout column sums recover their
    # base-table equivalents without re-scanning `analytics_events`.
    # The CTE materialises the filtered rollup window once and the
    # three result sets (totals, by_event, by_stage) union under a
    # `kind` discriminator. The previous standalone shape fired
    # three sequential queries that each re-scanned the events
    # table; the CTE collapses them and, by reading the rollup,
    # scans roughly orders of magnitude fewer rows once the events
    # table grows. The totals row carries every aggregate column;
    # the by_event / by_stage rows only populate `kind`, `label`,
    # and `count_val` -- the trailing NULLs keep the UNION-ALL
    # column shape uniform. Per-bucket ordering (`COUNT DESC,
    # label ASC`, matching the previous standalone queries) is
    # reasserted in Python so the planner is free to pick a
    # hash-aggregate / merge plan rather than being forced into a
    # sort.
    sql = (
        "WITH win AS ("
        "SELECT event, stage, repo, issue, "
        "event_count, failed_count, timed_out_count, "
        "total_cost_usd, total_input_tokens, total_output_tokens, "
        "total_cache_read_tokens, total_cache_write_tokens "
        f"FROM {_DAILY_ROLLUP_VIEW}{where}"
        ") "
        "SELECT 't' AS kind, NULL::text AS label, "
        "COALESCE(SUM(event_count), 0) AS count_val, "
        # `(repo, issue)` row-constructor: GitHub issue numbers are
        # only unique within a repo, so a multi-repo window would
        # otherwise collapse `owner/a#1` and `owner/b#1` into one.
        # The rollup key carries `(repo, issue)` so distinct counts
        # are still exact against the materialised view.
        "COUNT(DISTINCT (repo, issue)) AS distinct_issues, "
        "COUNT(DISTINCT repo) AS distinct_repos, "
        "COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd, "
        "COALESCE(SUM(total_input_tokens), 0) AS total_input_tokens, "
        "COALESCE(SUM(total_output_tokens), 0) AS total_output_tokens, "
        # Agent-run counters: scoped to `event = 'agent_exit'` rows
        # so the dashboard's success-rate metric reads off the same
        # query as the rest of the overview. The rollup's
        # `failed_count` predicate (`exit_code IS NOT NULL AND
        # exit_code <> 0`) already excludes NULL exit codes, and
        # `event = 'agent_exit'` narrows away any non-exit row that
        # happens to carry a non-null exit code.
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN event_count ELSE 0 END), 0) "
        "  AS total_agent_runs, "
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN failed_count ELSE 0 END), 0) "
        "  AS failed_agent_runs, "
        # Cache-band token rollups so the redesigned KPI strip and
        # sparkline can include them in the "Total tokens" headline
        # (matching the standalone mock's
        # `input + output + cache_read + cache_write` accounting).
        "COALESCE(SUM(total_cache_read_tokens), 0) "
        "  AS total_cache_read_tokens, "
        "COALESCE(SUM(total_cache_write_tokens), 0) "
        "  AS total_cache_write_tokens, "
        # Window-wide timeout counter. The rollup's `timed_out_count`
        # predicate is already scoped to `event = 'agent_exit' AND
        # timed_out = TRUE`, so a plain SUM recovers the previous
        # base-table aggregate without an extra `CASE` here.
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
    rows = _query(connect_fn, url, sql, params, conn=conn)
    if not rows:
        # Empty fake cursor; the real query always returns the
        # totals row even when the window is empty (aggregate over
        # zero rows yields zeros), but guard so a fixture that omits
        # everything never raises on the unpack below.
        return Summary()

    totals_row: Optional[tuple] = None
    by_event_pairs: list[tuple[str, int]] = []
    by_stage_pairs: list[tuple[str, int]] = []
    for row in rows:
        if not row:
            continue
        kind = row[0]
        if kind == "t":
            totals_row = row
        elif kind == "e" and row[1] is not None:
            by_event_pairs.append((row[1], int(row[2] or 0)))
        elif kind == "s" and row[1] is not None:
            by_stage_pairs.append((row[1], int(row[2] or 0)))

    # Reassert the `c DESC, label ASC` ordering the standalone
    # queries used to enforce in SQL so the dashboard sees the same
    # iteration order regardless of which UNION-ALL plan Postgres
    # picks.
    by_event_pairs.sort(key=lambda kv: (-kv[1], kv[0]))
    by_stage_pairs.sort(key=lambda kv: (-kv[1], kv[0]))
    by_event = {label: c for label, c in by_event_pairs}
    by_stage = {label: c for label, c in by_stage_pairs}

    if totals_row is None:
        return Summary(by_event=by_event, by_stage=by_stage)

    # The combined SQL guarantees a 13-column totals row, but
    # fixtures that pre-date the agent-run / cache-token / timeout
    # extensions may still emit shorter tuples; default the missing
    # columns to zero so the test harness does not have to know
    # about every new SQL column in unrelated cases. Column layout:
    # 0=kind, 1=label, 2=total_events, 3=distinct_issues,
    # 4=distinct_repos, 5=total_cost_usd, 6=total_input_tokens,
    # 7=total_output_tokens, 8=total_agent_runs,
    # 9=failed_agent_runs, 10=total_cache_read_tokens,
    # 11=total_cache_write_tokens, 12=timed_out_agent_runs.
    total_events = totals_row[2]
    distinct_issues = totals_row[3]
    distinct_repos = totals_row[4]
    total_cost_usd = totals_row[5]
    total_input_tokens = totals_row[6]
    total_output_tokens = totals_row[7]
    total_agent_runs = totals_row[8] if len(totals_row) > 8 else 0
    failed_agent_runs = totals_row[9] if len(totals_row) > 9 else 0
    total_cache_read_tokens = totals_row[10] if len(totals_row) > 10 else 0
    total_cache_write_tokens = totals_row[11] if len(totals_row) > 11 else 0
    timed_out_agent_runs = totals_row[12] if len(totals_row) > 12 else 0

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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return Summary()
    connect_fn = connect or _default_connect
    where, params = _build_rollup_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
    sql = (
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
    rows = _query(connect_fn, url, sql, params, conn=conn)
    if not rows:
        return Summary()
    row = rows[0]
    return Summary(
        total_cost_usd=float(row[0] or 0.0),
        total_input_tokens=int(row[1] or 0),
        total_output_tokens=int(row[2] or 0),
        total_cache_read_tokens=int(row[3] or 0),
        total_cache_write_tokens=int(row[4] or 0),
        total_agent_runs=int(row[5] or 0) if len(row) > 5 else 0,
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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    connect_fn = connect or _default_connect
    where, params = _build_rollup_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
    # Reads directly from the daily rollup: `day` is the GROUP BY
    # key the view is keyed on, so a per-day per-event aggregate
    # collapses to a tiny scan compared with the equivalent
    # `date_trunc('day', ts)` over the events table.
    sql = (
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
        "ORDER BY day ASC, event ASC"
    )
    rows = _query(connect_fn, url, sql, params, conn=conn)
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
    conn: Any = None,
) -> list[StageBreakdown]:
    """Per-stage counts, average handler duration, and cost rollups.

    Only counts rows whose `stage` is non-null (the partial-index
    case in the schema). Returns an empty list when the DB URL is
    unset or no row in the window carries a stage. The cost / token
    columns are summed across the stage so the breakdown can plot
    "spend per stage" without a second query.
    """
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    connect_fn = connect or _default_connect
    where, params = _build_rollup_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
    clause = (
        f"{where} AND stage IS NOT NULL"
        if where
        else " WHERE stage IS NOT NULL"
    )
    # Reads from the daily rollup. `duration_s_sum` / `duration_s_count`
    # are the prerequisites for `AVG(duration_s)` -- averaging averages
    # across days does not preserve the row-weighted mean, so the
    # rollup carries the sum and the non-NULL count separately and
    # the reader recovers `AVG` as `SUM(sum) / SUM(count)` here.
    # `NULLIF` keeps the denominator-NULL case (no row in the window
    # carried a duration) returning NULL rather than raising.
    #
    # Each rollup row is split into a cache portion (the share of its
    # tokens billed as cached / cache-read / cache-write) and a
    # no-cache portion (the remaining input + output tokens). Cost is
    # attributed proportionally so the per-stage chart shows what
    # fraction of spend flowed through the cache vs ran against fresh
    # tokens -- mirroring `get_review_round_breakdown`'s per-row
    # proration. Codex `cached_tokens` is a subset of `input_tokens`,
    # so it stays out of the denominator to avoid double-counting;
    # Claude `cache_read_tokens` / `cache_write_tokens` are reported
    # alongside `input_tokens` and so add to the denominator normally.
    # Proration is per rollup row -- one `(day, repo, issue, event,
    # stage, backend, cost_source)` bucket -- which is the finest
    # granularity available without bypassing the rollup.
    cache_tokens_expr = (
        "(COALESCE(total_cached_tokens, 0) "
        "+ COALESCE(total_cache_read_tokens, 0) "
        "+ COALESCE(total_cache_write_tokens, 0))"
    )
    all_tokens_expr = (
        "(COALESCE(total_input_tokens, 0) "
        "+ COALESCE(total_output_tokens, 0) "
        "+ COALESCE(total_cache_read_tokens, 0) "
        "+ COALESCE(total_cache_write_tokens, 0))"
    )
    cache_fraction_expr = (
        f"CASE WHEN {all_tokens_expr} = 0 THEN 0 "
        f"ELSE {cache_tokens_expr}::numeric / {all_tokens_expr}::numeric "
        f"END"
    )
    sql = (
        "SELECT stage, "
        "COALESCE(SUM(event_count), 0) AS c, "
        "SUM(duration_s_sum) / NULLIF(SUM(duration_s_count), 0) "
        "  AS avg_dur, "
        "COALESCE(SUM(total_cost_usd), 0) AS stage_cost_usd, "
        "COALESCE(SUM(total_input_tokens), 0) AS stage_input_tokens, "
        "COALESCE(SUM(total_output_tokens), 0) AS stage_output_tokens, "
        # Agent-run subset of `count`: the rollup carries `event_count`
        # per `(day, repo, issue, event, stage, backend, cost_source)`
        # bucket, so summing `event_count` over the agent_exit slice
        # recovers the per-stage run count without double-counting
        # rows the way a `COUNT(*)` on the rollup table would.
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN event_count ELSE 0 END), 0) "
        "  AS stage_agent_runs, "
        "COALESCE(SUM(COALESCE(total_cost_usd, 0) "
        f"* ({cache_fraction_expr})), 0) AS stage_cache_cost_usd, "
        "COALESCE(SUM(COALESCE(total_cost_usd, 0) "
        f"* (1 - ({cache_fraction_expr}))), 0) AS stage_no_cache_cost_usd "
        f"FROM {_DAILY_ROLLUP_VIEW}{clause} "
        "GROUP BY stage ORDER BY c DESC, stage ASC"
    )
    rows = _query(connect_fn, url, sql, params, conn=conn)
    out: list[StageBreakdown] = []
    for row in rows:
        stage = row[0]
        count = row[1]
        avg_dur = row[2]
        cost = row[3] if len(row) > 3 else 0.0
        in_tok = row[4] if len(row) > 4 else 0
        out_tok = row[5] if len(row) > 5 else 0
        runs = row[6] if len(row) > 6 else 0
        # Older fixtures may still emit rows without the cache split;
        # default those columns so unrelated tests round-trip.
        cache_cost = row[7] if len(row) > 7 else 0.0
        no_cache_cost = row[8] if len(row) > 8 else 0.0
        out.append(
            StageBreakdown(
                stage=stage,
                count=int(count),
                avg_duration_s=float(avg_dur) if avg_dur is not None else None,
                total_cost_usd=float(cost or 0.0),
                total_input_tokens=int(in_tok or 0),
                total_output_tokens=int(out_tok or 0),
                runs=int(runs or 0),
                cache_cost_usd=float(cache_cost or 0.0),
                no_cache_cost_usd=float(no_cache_cost or 0.0),
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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    if _agent_event_excluded(events):
        return []
    connect_fn = connect or _default_connect
    where, params = _build_rollup_window_where(
        start=start, end=end, repo=repo,
        events=None, stages=stages, issue=issue,
    )
    clause = (
        f"{where} AND event = 'agent_exit'"
        if where
        else " WHERE event = 'agent_exit'"
    )
    sql = (
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
    rows = _query(connect_fn, url, sql, params, conn=conn)
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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
        return []
    connect_fn = connect or _default_connect
    where, params = _build_rollup_window_where(
        start=start, end=end, repo=repo,
        events=events, stages=stages, issue=issue,
    )
    sql = (
        "SELECT repo, "
        "COUNT(DISTINCT issue) AS repo_issues, "
        "COALESCE(SUM(event_count), 0) AS repo_events, "
        "COALESCE(SUM(CASE WHEN event = 'agent_exit' "
        "                  THEN event_count ELSE 0 END), 0) "
        "  AS repo_agent_exits, "
        "COALESCE(SUM(total_cost_usd), 0) AS repo_cost_usd "
        f"FROM {_DAILY_ROLLUP_VIEW}{where} "
        "GROUP BY repo "
        "ORDER BY repo_events DESC, repo ASC"
    )
    rows = _query(connect_fn, url, sql, params, conn=conn)
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
    url = _resolve_db_url(db_url)
    if conn is None and not url:
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
        conditions.append("day >= %s")
        params.append(start.date() if isinstance(start, datetime) else start)
    if end is not None:
        conditions.append("day < %s")
        params.append(end.date() if isinstance(end, datetime) else end)
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
    # Reads from the daily rollup: `event_count` already collapses
    # multiple `stage_enter` rows for the same `(day, repo, issue,
    # stage, backend, cost_source)` bucket into one row, so summing
    # `event_count` per day per terminal stage recovers the prior
    # per-day `COUNT(*)` without re-scanning `analytics_events`.
    sql = (
        "SELECT day, "
        "COALESCE(SUM(CASE WHEN stage = 'done' "
        "                  THEN event_count ELSE 0 END), 0) AS resolved, "
        "COALESCE(SUM(CASE WHEN stage = 'rejected' "
        "                  THEN event_count ELSE 0 END), 0) AS rejected "
        f"FROM {_DAILY_ROLLUP_VIEW}{where} "
        "GROUP BY day "
        "ORDER BY day ASC"
    )
    rows = _query(connect_fn, url, sql, params, conn=conn)
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
