# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Postgres read model for the `analytics_events` table.

This package layer is a thin, testable data-access layer over the
schema defined in `analytics-db/init/01-schema.sql` and populated by
`orchestrator.analytics.sync`. It exposes plain-Python functions for
the shapes a dashboard needs (filter dropdowns, date-bounded summary
counts, daily time-series, stage / event breakdowns, the most recent
agent-exit rows, per-issue event traces, and the chart-shaped
breakdowns the redesigned dashboard renders -- review-round buckets,
per-backend efficiency, per-repo rollups, cost-source coverage, and
the weekday x hour activity heatmap) without taking on the
Streamlit / web layer itself -- that lives in
`orchestrator/dashboard.py`.

This module is the public facade: it imports nothing of its own and
re-exports the reader functions, their frozen read-model
dataclasses, the connection / error plumbing, and the issue-sort
constants so the `orchestrator.analytics.read` import surface every
caller already depends on stays unchanged. The implementation is
split into focused sibling modules:

- `read_raw` -- foundational readers over `analytics_events` /
  `analytics_agent_runs`: filter options, data extent, the per-event
  count breakdown, the newest agent-exit rows, the
  one-row-per-`(repo, issue)` overview, and the per-issue event
  trace.
- `read_rollup` -- the window-bounded aggregates the
  `analytics_daily_rollup` materialised view can reconstruct
  exactly: `get_summary`, `get_kpi_prev`, `get_time_series`,
  `get_stage_breakdown`, `get_backend_efficiency`,
  `get_repo_breakdown`, `get_throughput_breakdown`.
- `read_dashboard` -- the redesigned-dashboard chart breakdowns the
  rollup cannot reconstruct (they need row-level detail or columns
  the rollup omits): `get_review_round_breakdown`,
  `get_skill_trigger_rates`, `get_cost_coverage`,
  `get_backend_daily_tokens`, `get_hourly_heatmap`.

The supporting plumbing is split into further sibling modules and
re-exported here as well:

- `read_models` -- the frozen read-model dataclasses each helper
  returns.
- `connection` -- `AnalyticsReadError`, the deferred-psycopg connect
  factories, and the thread-local persistent-connection cache
  (`analytics_connection` / `close_thread_local_connection`).
- `db_url` -- `_resolve_db_url`, the single `db_url=` ->
  `analytics.ANALYTICS_DB_URL` fallback.
- `query` -- `_query`, the single-SELECT execution path.
- `predicates` -- the window / filter `WHERE`-clause builders shared
  across the helpers.

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

`analytics_agent_runs` is a view over `event = 'agent_exit'` rows
defined in the schema; its derivations (`review_round_bucket`,
`failed`, `model`, `total_tokens`, `has_cost`) are what the
per-row agent-run aggregates in `read_dashboard` query against. The
view has no `event` column -- the predicate is baked in -- so
functions that read from the view honor the event filter by
short-circuiting to empty when the operator's events selection
excludes `agent_exit` rather than emitting an `event IN (...)`
clause that would refer to a non-existent column.

The dashboard's window-bounded aggregate widgets read from a
separate materialised view, `analytics_daily_rollup`, which carries
the per-`(day, repo, issue, event, stage, backend, cost_source)`
aggregates the orchestrator's sync job refreshes after every
successful commit. Reading from the rollup collapses the
events-table scan to a tiny day-keyed scan once the events table
grows. The cutover covers `get_summary`, `get_kpi_prev`,
`get_time_series`, `get_stage_breakdown`, `get_repo_breakdown`,
`get_backend_efficiency`, and `get_throughput_breakdown` -- every
shape whose aggregates the rollup can reconstruct exactly (all in
`read_rollup`). The per-row helpers (`get_recent_agent_exits`,
`get_issues` / top-cost-issues, `get_issue_events`,
`get_review_round_breakdown`, `get_hourly_heatmap`,
`get_cost_coverage`, plus the still-view-backed
`get_backend_daily_tokens` and `get_event_breakdown`) keep reading
from `analytics_events` or `analytics_agent_runs` because they need
row-level detail or aggregate columns the rollup does not carry
(`ts` precision, `review_round`, `retry_count`, `hour-of-day`).
"""
from __future__ import annotations

from .connection import (
    AnalyticsReadError as AnalyticsReadError,
    analytics_connection as analytics_connection,
    close_thread_local_connection as close_thread_local_connection,
    _close_quietly as _close_quietly,
    _default_connect as _default_connect,
    _default_persistent_connect as _default_persistent_connect,
    _is_broken_connection_exc as _is_broken_connection_exc,
    _thread_local as _thread_local,
)
from .read_dashboard import (
    get_backend_daily_tokens as get_backend_daily_tokens,
    get_cost_coverage as get_cost_coverage,
    get_hourly_heatmap as get_hourly_heatmap,
    get_review_round_breakdown as get_review_round_breakdown,
    get_skill_trigger_rates as get_skill_trigger_rates,
)
from .read_models import (
    AgentExitRow as AgentExitRow,
    BackendDailyTokensRow as BackendDailyTokensRow,
    BackendEfficiencyRow as BackendEfficiencyRow,
    CostCoverageRow as CostCoverageRow,
    DataExtent as DataExtent,
    EventBreakdown as EventBreakdown,
    FilterOptions as FilterOptions,
    HourlyHeatmapPoint as HourlyHeatmapPoint,
    IssueEventRow as IssueEventRow,
    IssueSummaryRow as IssueSummaryRow,
    RepoBreakdownRow as RepoBreakdownRow,
    ReviewRoundBucketRow as ReviewRoundBucketRow,
    SkillTriggerRateRow as SkillTriggerRateRow,
    StageBreakdown as StageBreakdown,
    Summary as Summary,
    ThroughputDayRow as ThroughputDayRow,
    TimeSeriesPoint as TimeSeriesPoint,
)
from .read_raw import (
    SORT_BY_COST as SORT_BY_COST,
    SORT_BY_LAST_SEEN as SORT_BY_LAST_SEEN,
    get_data_extent as get_data_extent,
    get_event_breakdown as get_event_breakdown,
    get_filter_options as get_filter_options,
    get_issue_events as get_issue_events,
    get_issues as get_issues,
    get_recent_agent_exits as get_recent_agent_exits,
)
from .read_rollup import (
    get_backend_efficiency as get_backend_efficiency,
    get_kpi_prev as get_kpi_prev,
    get_repo_breakdown as get_repo_breakdown,
    get_stage_breakdown as get_stage_breakdown,
    get_summary as get_summary,
    get_throughput_breakdown as get_throughput_breakdown,
    get_time_series as get_time_series,
)
