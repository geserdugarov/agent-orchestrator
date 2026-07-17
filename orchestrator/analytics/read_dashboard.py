# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard-facing aggregate readers the daily rollup cannot reconstruct.

The chart-shaped breakdowns the redesigned dashboard renders that
read `analytics_events` / `analytics_agent_runs` directly because
they need row-level detail or columns the daily rollup does not
carry: per-review-round development/review buckets (raw
`review_round`), per-`(agent_role, backend)` skill-trigger rates and
the per-skill `(repo, agent_role, backend)` trigger matrix (both off
the `extras` JSONB the rollup omits, the matrix folding in the
`repo_skill_catalog` records too), per-`cost_source` coverage,
per-`(day, backend)` token totals, and the weekday x hour activity
heatmap (hour-of-day precision the day-keyed rollup loses).

Re-exported unchanged through `orchestrator.analytics.read`; see
that module's docstring for the connection / URL / error contract
and the agent-run event-filter short-circuit these helpers share.
Raw-table overview readers live in `read_raw`; the rollup-backed
aggregates in `read_rollup`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from orchestrator.analytics.predicates import (
    _WindowFilters,
    _agent_event_excluded,
    _append_where_condition,
    _build_view_window_where,
    _build_window_where,
)
from orchestrator.analytics.query import _ReadQuery
from orchestrator.analytics.read_models import (
    BackendDailyTokensRow,
    CostCoverageRow,
    HourlyHeatmapPoint,
    ReviewRoundBucketRow,
    SkillTriggerMatrixRow,
    SkillTriggerRateRow,
)


def _as_skill_names(raw: Any) -> list[str]:
    """Coerce a JSONB skill-name array column into a list of strings.

    psycopg adapts a `jsonb` array to a Python list, so the common path
    is a passthrough; a driver / fixture that hands back the raw JSON
    text is tolerated too. ``None`` (the absent-key result of
    ``extras -> 'skills_...'``), a non-list payload, or a non-string
    element collapses to an empty list / is skipped so a malformed
    `extras` blob never raises mid-read.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [name for name in raw if isinstance(name, str)]


def _label_or_unknown(raw: Any) -> str:
    if raw is None:
        return "unknown"
    return str(raw)


def _row_label(row: Sequence[Any], index: int) -> str:
    if len(row) <= index:
        return "unknown"
    return _label_or_unknown(row[index])


def _skill_matrix_order_key(
    key: tuple[str, str, str, str],
    *,
    counts: dict[tuple[str, str, str, str], int],
    cohort_runs: dict[tuple[str, str, str], int],
) -> tuple:
    repo, role, backend, skill = key
    return (
        -counts.get(key, 0),
        -cohort_runs.get((repo, role, backend), 0),
        repo,
        role,
        backend,
        skill,
    )


def _row_value(row: Sequence[Any], index: int, default: Any = 0) -> Any:
    if len(row) <= index:
        return default
    return row[index]


def _cost_cell(row: Sequence[Any], index: int) -> float:
    """Read a nullable USD cost column as a float, treating null/missing as zero."""
    return float(_row_value(row, index) or 0)


def _day_value(day: Any) -> Any:
    if isinstance(day, datetime):
        return day.date()
    return day


_AGENT_CACHE_TOKENS_SQL = (
    "(COALESCE(cached_tokens, 0) "
    "+ COALESCE(cache_read_tokens, 0) "
    "+ COALESCE(cache_write_tokens, 0))"
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


# Default cap on the rows `get_skill_trigger_matrix` returns. The
# dashboard renders the matrix in a fold-out expander; capping keeps an
# expand from flooding the page when many repos x cohorts x catalog
# skills multiply out. A non-positive `limit` disables the cap.
SKILL_MATRIX_ROW_LIMIT = 100


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
    return ReviewRoundBucketRow(
        bucket=str(row[0]),
        runs=int(row[1] or 0),
        failed=int(row[2] or 0),
        total_cost_usd=_cost_cell(row, 3),
        developer_runs=int(_row_value(row, 4) or 0),
        reviewer_runs=int(_row_value(row, 5) or 0),
        developer_cost_usd=_cost_cell(row, 6),
        reviewer_cost_usd=_cost_cell(row, 7),
        developer_cache_cost_usd=_cost_cell(row, 8),
        developer_no_cache_cost_usd=_cost_cell(row, 9),
        reviewer_cache_cost_usd=_cost_cell(row, 10),
        reviewer_no_cache_cost_usd=_cost_cell(row, 11),
    )


def _review_round_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[ReviewRoundBucketRow]:
    where, bindings = _build_view_window_where(filters)
    where = _append_where_condition(
        where,
        "agent_role IN ('developer', 'reviewer')",
    )
    rows = query.select(_review_round_sql(where), bindings)
    return [_review_round_from_row(row) for row in rows]


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
    return _review_round_rows(query, filters)


def _skill_trigger_rate_sql(clause: str) -> str:
    return (
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


def _skill_trigger_rate_from_row(row: Sequence[Any]) -> SkillTriggerRateRow:
    return SkillTriggerRateRow(
        agent_role=_label_or_unknown(row[0]),
        backend=_label_or_unknown(row[1]),
        runs=int(row[2] or 0),
        skill_runs=int(_row_value(row, 3) or 0),
        total_triggers=int(_row_value(row, 4) or 0),
    )


def _skill_trigger_rate_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[SkillTriggerRateRow]:
    where, bindings = _build_window_where(filters.without_events())
    clause = _append_where_condition(where, "event = 'agent_exit'")
    rows = query.select(_skill_trigger_rate_sql(clause), bindings)
    return [_skill_trigger_rate_from_row(row) for row in rows]


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
    return _skill_trigger_rate_rows(query, filters)


_SkillCohort = tuple[str, str, str]
_SkillMatrixKey = tuple[str, str, str, str]


def _skill_catalog_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[tuple]:
    where, bindings = _build_window_where(filters.catalog_scope())
    clause = _append_where_condition(where, "event = 'repo_skill_catalog'")
    return query.select(
        "SELECT repo, extras -> 'skills_available' AS skills_available "
        f"FROM analytics_events{clause}",
        bindings,
    )


def _skill_catalog(rows: Sequence[tuple]) -> dict[str, set[str]]:
    catalog: dict[str, set[str]] = {}
    for row in rows:
        if row[0] is None:
            continue
        repo = str(row[0])
        names = _as_skill_names(_row_value(row, 1, None))
        catalog.setdefault(repo, set()).update(names)
    return catalog


def _skill_run_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[tuple]:
    where, bindings = _build_window_where(filters.without_events())
    clause = _append_where_condition(where, "event = 'agent_exit'")
    return query.select(
        "SELECT repo, "
        "COALESCE(agent_role, 'unknown') AS role_label, "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "extras -> 'skills_triggered' AS skills_triggered "
        f"FROM analytics_events{clause}",
        bindings,
    )


def _skill_cohort(row: Sequence[Any]) -> _SkillCohort:
    return (
        _label_or_unknown(row[0]),
        _row_label(row, 1),
        _row_label(row, 2),
    )


@dataclass
class _SkillMatrixCounts:
    """Run and trigger counts used to assemble the skill matrix."""

    cohort_runs: dict[_SkillCohort, int] = field(default_factory=dict)
    skill_runs: dict[_SkillMatrixKey, int] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: Sequence[tuple]) -> _SkillMatrixCounts:
        counts = cls()
        for row in rows:
            cohort = _skill_cohort(row)
            counts.cohort_runs[cohort] = counts.cohort_runs.get(cohort, 0) + 1
            for skill in set(_as_skill_names(_row_value(row, 3, None))):
                key = (*cohort, skill)
                counts.skill_runs[key] = counts.skill_runs.get(key, 0) + 1
        return counts

    def matrix_keys(
        self,
        catalog: dict[str, set[str]],
    ) -> set[_SkillMatrixKey]:
        keys = set(self.skill_runs)
        for cohort in self.cohort_runs:
            for skill in catalog.get(cohort[0], ()):
                keys.add((*cohort, skill))
        return keys

    def order_key(self, key: _SkillMatrixKey) -> tuple:
        return _skill_matrix_order_key(
            key,
            counts=self.skill_runs,
            cohort_runs=self.cohort_runs,
        )

    def as_row(self, key: _SkillMatrixKey) -> SkillTriggerMatrixRow:
        repo, role, backend, skill = key
        return SkillTriggerMatrixRow(
            repo=repo,
            skill=skill,
            agent_role=role,
            backend=backend,
            runs=self.cohort_runs.get((repo, role, backend), 0),
            skill_runs=self.skill_runs.get(key, 0),
        )


def _skill_trigger_matrix_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
    limit: int,
) -> list[SkillTriggerMatrixRow]:
    catalog = _skill_catalog(_skill_catalog_rows(query, filters))
    counts = _SkillMatrixCounts.from_rows(_skill_run_rows(query, filters))
    keys = sorted(counts.matrix_keys(catalog), key=counts.order_key)
    if limit > 0:
        keys = keys[:limit]
    return [counts.as_row(key) for key in keys]


def get_skill_trigger_matrix(
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    repo: Optional[str] = None,
    events: Optional[Sequence[str]] = None,
    stages: Optional[Sequence[str]] = None,
    issue: Optional[int] = None,
    limit: int = SKILL_MATRIX_ROW_LIMIT,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
    conn: Any = None,
) -> list[SkillTriggerMatrixRow]:
    """Per-skill x `(repo, agent_role, backend)` trigger-run counts.

    Combines the repo's `repo_skill_catalog` records (the universe of
    skills a repo offers, via the `skills_available` array) with the
    filtered `agent_exit` rows (the runs that actually fired a skill,
    via the `skills_triggered` array) so the dashboard can render a
    matrix of which skills each cohort reaches for. Both arrays live in
    `analytics_events.extras` JSONB -- the daily rollup does not carry
    them -- so the reader scans the base table directly: a pure
    read-side addition with zero DDL, mirroring `get_skill_trigger_rates`.

    Honors the same `agent_exit` event-filter contract as the other
    skill / agent-run readers: short-circuits to empty (no DB round
    trip at all, catalog included) when the events multiselect excludes
    `agent_exit` or is cleared. The date / repo filters narrow *both*
    the catalog and the run queries; the stage / issue filters narrow
    only the runs because catalog records are repo-level (they carry
    `issue = 0` and a NULL stage, so pushing those predicates down would
    drop every catalog row).

    Each cell carries two counts. `skill_runs` counts runs *containing*
    that skill -- one per agent-exit row per distinct name in its
    `skills_triggered` list -- rather than total invocations, so a run
    that pulled `develop` three times still weighs one. `runs` is the
    total agent-exit runs in the cell's `(repo, agent_role, backend)`
    cohort, so a low `skill_runs` reads against the cohort size. Every
    catalog skill is zero-padded across the cohorts observed for that
    repo so the matrix carries explicit `developer / claude / review =
    0` (`skill_runs == 0`) cells for offered-but-untriggered skills.
    NULL `agent_role` / `backend` bucket under `"unknown"`. When no
    catalog records match the window the matrix degrades cleanly to just
    the observed-trigger cells -- no zero rows are invented.

    Rows are ordered by `skill_runs` DESC, then cohort `runs` DESC, then
    a stable `(repo, agent_role, backend, skill)` tiebreak, and the list
    is capped at `limit` rows (default `SKILL_MATRIX_ROW_LIMIT`; a
    non-positive `limit` disables the cap) so the dashboard's fold-out
    never floods the page.
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
    return _skill_trigger_matrix_rows(query, filters, limit)


def _cost_coverage_from_row(row: Sequence[Any]) -> CostCoverageRow:
    return CostCoverageRow(
        cost_source=str(row[0]),
        runs=int(row[1] or 0),
        total_tokens=int(_row_value(row, 2) or 0),
    )


def _cost_coverage_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[CostCoverageRow]:
    where, bindings = _build_view_window_where(filters)
    rows = query.select(
        "SELECT "
        "COALESCE(cost_source, 'unknown') AS source_label, "
        "COUNT(*) AS runs, "
        "COALESCE(SUM("
        "  COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + "
        "  COALESCE(cache_read_tokens, 0) + "
        "  COALESCE(cache_write_tokens, 0)"
        "), 0) AS source_total_tokens "
        f"FROM analytics_agent_runs{where} "
        "GROUP BY source_label "
        "ORDER BY runs DESC, source_label ASC",
        bindings,
    )
    return [_cost_coverage_from_row(row) for row in rows]


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
    return _cost_coverage_rows(query, filters)


def _backend_daily_tokens_from_row(
    row: Sequence[Any],
) -> BackendDailyTokensRow:
    return BackendDailyTokensRow(
        day=_day_value(row[0]),
        backend=str(row[1]),
        total_tokens=int(row[2] or 0),
    )


def _backend_daily_token_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
) -> list[BackendDailyTokensRow]:
    where, bindings = _build_view_window_where(filters)
    rows = query.select(
        "SELECT "
        "date_trunc('day', ts)::date AS day, "
        "COALESCE(backend, 'unknown') AS backend_label, "
        "COALESCE(SUM("
        "  COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + "
        "  COALESCE(cache_read_tokens, 0) + "
        "  COALESCE(cache_write_tokens, 0)"
        "), 0) AS day_backend_tokens "
        f"FROM analytics_agent_runs{where} "
        "GROUP BY day, backend_label "
        "ORDER BY day ASC, backend_label ASC",
        bindings,
    )
    return [_backend_daily_tokens_from_row(row) for row in rows]


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
    return _backend_daily_token_rows(query, filters)


def _hourly_heatmap_from_row(row: Sequence[Any]) -> HourlyHeatmapPoint:
    return HourlyHeatmapPoint(
        weekday=int(row[0]),
        hour=int(row[1]),
        count=int(row[2] or 0),
        total_tokens=int(_row_value(row, 3) or 0),
    )


def _hourly_heatmap_rows(
    query: _ReadQuery,
    filters: _WindowFilters,
    tz_offset_hours: int,
) -> list[HourlyHeatmapPoint]:
    where, bindings = _build_window_where(filters)
    offset = int(tz_offset_hours)
    rows = query.select(
        "SELECT "
        "EXTRACT(DOW FROM ((ts AT TIME ZONE 'UTC') "
        "+ %s * INTERVAL '1 hour'))::int AS weekday, "
        "EXTRACT(HOUR FROM ((ts AT TIME ZONE 'UTC') "
        "+ %s * INTERVAL '1 hour'))::int AS hour, "
        "COUNT(*) AS c, "
        "COALESCE(SUM("
        "  COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) + "
        "  COALESCE(cache_read_tokens, 0) + "
        "  COALESCE(cache_write_tokens, 0)"
        "), 0) AS cell_total_tokens "
        f"FROM analytics_events{where} "
        "GROUP BY weekday, hour "
        "ORDER BY weekday ASC, hour ASC",
        [offset, offset, *bindings],
    )
    return [_hourly_heatmap_from_row(row) for row in rows]


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
    return _hourly_heatmap_rows(query, filters, tz_offset_hours)
