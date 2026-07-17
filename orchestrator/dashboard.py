# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Streamlit analytics dashboard -- page orchestration.

Renders the redesigned `Orchestrator Analytics` page (#341) over the
read model populated by `orchestrator.analytics.sync`. The layout
mirrors the standalone HTML mock the issue ships:

- A top bar with the page title, the data extent / repo / event
  summary, and the in-range spend pill.
- A filter bar carrying the `3D` / `7D` / `All` preset selector and
  the two-date custom range.
- Computed insight banners (failure rate, unpriced cost coverage).
- A four-tile KPI strip (total spend, total tokens, cost / resolved
  issue, rework share) with previous-window deltas.
- A grid of cards: hero spend / token usage stacked-area chart,
  per-stage cost bars, per-review-cycle cost bars, top-cost issues
  table, per-backend efficiency cards + cost-source coverage bar,
  per-repo cost bars, reliability tiles + resolved-per-day chart,
  weekday-by-hour activity heatmap.
- Per-issue drill-down at the bottom when an issue number is
  entered in the sidebar.

The pure helpers behind this page live in focused modules so this
file stays the Streamlit orchestration layer:

- `orchestrator.dashboard_state` -- date / window math, preset and
  timezone vocabulary, stage-filter / cache-key resolution, the
  issue-number parser, the DB-config banner check, and the read
  fan-out switch.
- `orchestrator.dashboard_kpis` -- KPI delta math, the computed
  insight banners, the reliability-tile triples, the top-cost issue
  ordering, and the rework-share aggregation.
- `orchestrator.dashboard_html` -- the inline-HTML builders for the
  topbar, filter meta, KPI strip, insight stack, per-card header,
  sparkline / delta pill, the issues / skill-trigger tables, the
  per-skill trigger matrix, the backend-efficiency card, the
  cost-coverage bar, and the reliability-tile strip.

`main()` is the lazy Streamlit entrypoint. The page pipeline below it
groups the imported dashboard modules, resolved filters, read waves,
and KPI results into small immutable state objects. Focused helpers own
static metadata, controls, staged reads, empty states, and card sections,
so the two-wave render order remains explicit without one oversized
entrypoint.

Every pure helper from those three modules is re-exported below under
its original name so `streamlit run orchestrator/dashboard.py`, the
historical `orchestrator.dashboard.*` helper surface, and the existing
dashboard tests keep working without touching the extracted modules.
The page-pipeline helpers are module-private. `_render_drilldown` is the
one exception: its historical signature remains on the explicit export
surface and delegates to the typed internal drill-down renderer.

Reads go through `orchestrator.analytics.read` (which already
handles unset DB, connection errors, and lazy psycopg import) and
are wrapped in `st.cache_data` keyed by `(start, end, repo, events,
stages, issue)` so every widget sees the same window. The data-
extent and filter-option reads have no filter inputs and are cached
under a longer 5-minute TTL (`STATIC_METADATA_TTL_SECONDS`) so the
sidebar / topbar do not re-pay a fresh round-trip on every rerun.

The widget reads are dispatched in two staged waves so the topbar
and KPI strip paint as soon as their inputs are available instead
of blocking on every widget: the first wave covers `summary`,
`prev_summary`, `ts_points`, `throughput_rows`, `review_round_rows`,
and `cost_coverage_rows` (the only reads the topbar / filter meta
/ insight banners / KPI strip consume), and the second wave covers
the nine remaining widget reads (including the skill-trigger
aggregate and the per-skill trigger matrix). Worker threads only
return data back to the main render thread; every `st` / placeholder
write runs on the main thread.

Streamlit (and its transitive pandas), `plotly`, the chart builders
in `orchestrator.dashboard_charts`, and the theme tokens in
`orchestrator.dashboard_theme` are imported *lazily* on the `main()`
call path so the polling tick's `orchestrator.*` import surface stays free of
the dashboard's dependency footprint. The module loads without
`streamlit` or `plotly` installed -- only `streamlit run
orchestrator/dashboard.py` (or a direct `main()` call) materializes
the imports. The extracted helper modules are import-light (stdlib
plus `orchestrator.analytics`) so they preserve this invariant; it
is asserted by `tests/test_dashboard.py`.

Run:
    uv sync --group dashboard
    uv run streamlit run orchestrator/dashboard.py
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from time import perf_counter
from typing import Any, Callable, Optional, Sequence

# `streamlit run orchestrator/dashboard.py` launches this file as a
# top-level script with only `orchestrator/` on `sys.path`, so the repo
# root has to be added before the absolute imports below resolve;
# `orchestrator/script_launch.py` documents why. `__package__` selects the
# import per launch mode: a package import (`import orchestrator.dashboard`)
# sets it to `"orchestrator"` and takes the qualified import, so a stray
# top-level `script_launch` on `sys.path` cannot shadow the helper; a script
# launch leaves it empty/absent and takes the bare `import script_launch`,
# which loads the helper from the script's own directory WITHOUT importing
# the `orchestrator` package before the repo root is on the path (doing so
# would bind the parent to any stale/installed copy already importable).
if globals().get("__package__"):
    from orchestrator.script_launch import ensure_repo_root_on_path
else:  # script-launched: only `orchestrator/` is on sys.path
    from script_launch import ensure_repo_root_on_path

ensure_repo_root_on_path(__file__)

from orchestrator import analytics as analytics  # noqa: E402
from orchestrator.analytics import read as analytics_read  # noqa: E402
from orchestrator.analytics.read import (  # noqa: E402
    CostCoverageRow as CostCoverageRow,
    DataExtent as DataExtent,
    IssueSummaryRow as IssueSummaryRow,
    SkillTriggerMatrixRow as SkillTriggerMatrixRow,
    SkillTriggerRateRow as SkillTriggerRateRow,
    Summary as Summary,
)

# Compatibility re-exports. The pure helpers moved to the focused
# `dashboard_state` / `dashboard_kpis` / `dashboard_html` modules; we
# import each one back under its original name so `main()` calls them
# as bare names, the historical `orchestrator.dashboard.*` surface
# stays intact, and the existing tests (which reach the helpers via
# `dashboard.<name>` and inspect `main()`'s source) keep working. The
# redundant `as` alias marks each as an intentional re-export so ruff
# does not flag the unused import; the E402 suppression covers the
# post-`sys.path` placement the script-launch fix forces.
from orchestrator.dashboard_state import (  # noqa: E402
    DASHBOARD_PARALLEL_READS as DASHBOARD_PARALLEL_READS,
    DEFAULT_PRESET as DEFAULT_PRESET,
    DEFAULT_TZ_OFFSET_HOURS as DEFAULT_TZ_OFFSET_HOURS,
    DEFAULT_WINDOW_DAYS as DEFAULT_WINDOW_DAYS,
    DateWindow as DateWindow,
    PARALLEL_READS_ENV as PARALLEL_READS_ENV,
    PARALLEL_READS_MAX_WORKERS as PARALLEL_READS_MAX_WORKERS,
    PRESET_3D as PRESET_3D,
    PRESET_7D as PRESET_7D,
    PRESET_ALL as PRESET_ALL,
    PRESET_CUSTOM as PRESET_CUSTOM,
    PRESET_DAYS as PRESET_DAYS,
    PRESET_INLINE_LABELS as PRESET_INLINE_LABELS,
    PRESET_LABELS as PRESET_LABELS,
    PRESET_OPTIONS as PRESET_OPTIONS,
    TZ_OFFSET_OPTIONS as TZ_OFFSET_OPTIONS,
    UNCONFIGURED_DB_MESSAGE as UNCONFIGURED_DB_MESSAGE,
    _TRUTHY as _TRUTHY,
    _extent_dates as _extent_dates,
    _fan_out_reads as _fan_out_reads,
    _parse_parallel_reads_flag as _parse_parallel_reads_flag,
    cache_key as cache_key,
    dashboard_parallel_reads_enabled as dashboard_parallel_reads_enabled,
    db_unconfigured_message as db_unconfigured_message,
    default_date_range as default_date_range,
    format_tz_offset as format_tz_offset,
    parse_issue_number as parse_issue_number,
    preset_window as preset_window,
    previous_window as previous_window,
    resolve_stage_filter as resolve_stage_filter,
    shift_ts as shift_ts,
    to_window as to_window,
)
from orchestrator.dashboard_kpis import (  # noqa: E402
    DEFAULT_EXPENSIVE_LIMIT as DEFAULT_EXPENSIVE_LIMIT,
    FAILURE_RATE_BANNER_THRESHOLD as FAILURE_RATE_BANNER_THRESHOLD,
    REWORK_BUCKETS as REWORK_BUCKETS,
    UNPRICED_COST_SOURCES as UNPRICED_COST_SOURCES,
    UNPRICED_COVERAGE_THRESHOLD as UNPRICED_COVERAGE_THRESHOLD,
    InsightBanner as InsightBanner,
    compute_insights as compute_insights,
    kpi_delta as kpi_delta,
    reliability_tile_data as reliability_tile_data,
    rework_totals as rework_totals,
    top_expensive_issues as top_expensive_issues,
)
from orchestrator.dashboard_html import (  # noqa: E402
    _backend_efficiency_card_html as _backend_efficiency_card_html,
    _card_header_html as _card_header_html,
    _cost_coverage_bar_html as _cost_coverage_bar_html,
    _delta_pill as _delta_pill,
    _filter_meta_html as _filter_meta_html,
    _insights_html as _insights_html,
    _issues_table_html as _issues_table_html,
    _kpi_strip_html as _kpi_strip_html,
    _reliability_tiles_html as _reliability_tiles_html,
    _skill_matrix_html as _skill_matrix_html,
    _skill_triggers_html as _skill_triggers_html,
    _sparkline_svg as _sparkline_svg,
    _topbar_html as _topbar_html,
    parse_skill_matrix_sort as parse_skill_matrix_sort,
)

# Canonical inventory of the `orchestrator.dashboard.*` surface: the page
# entrypoint (`main`) and its drill-down helper, the page-level constants
# defined below, and every pure helper re-exported above from
# `dashboard_state` / `dashboard_kpis` / `dashboard_html` (plus the
# `analytics` / `analytics_read` module handles). Keeping the list explicit
# makes the compatibility surface auditable in one place and governs
# `from orchestrator.dashboard import *`.
__all__ = [
    "CostCoverageRow",
    "DASHBOARD_PARALLEL_READS",
    "DEFAULT_EXPENSIVE_LIMIT",
    "DEFAULT_PRESET",
    "DEFAULT_RECENT_AGENT_EXITS",
    "DEFAULT_TZ_OFFSET_HOURS",
    "DEFAULT_WINDOW_DAYS",
    "DataExtent",
    "DateWindow",
    "EMPTY_WINDOW_MESSAGE",
    "FAILURE_RATE_BANNER_THRESHOLD",
    "InsightBanner",
    "IssueSummaryRow",
    "LOADING_INDICATOR_MESSAGE",
    "NO_DATA_MESSAGE",
    "PARALLEL_READS_ENV",
    "PARALLEL_READS_MAX_WORKERS",
    "PLOTLY_CONFIG",
    "PRESET_3D",
    "PRESET_7D",
    "PRESET_ALL",
    "PRESET_CUSTOM",
    "PRESET_DAYS",
    "PRESET_INLINE_LABELS",
    "PRESET_LABELS",
    "PRESET_OPTIONS",
    "REWORK_BUCKETS",
    "STATIC_METADATA_TTL_SECONDS",
    "SkillTriggerMatrixRow",
    "SkillTriggerRateRow",
    "Summary",
    "TZ_OFFSET_OPTIONS",
    "UNCONFIGURED_DB_MESSAGE",
    "UNPRICED_COST_SOURCES",
    "UNPRICED_COVERAGE_THRESHOLD",
    "_TRUTHY",
    "_backend_efficiency_card_html",
    "_card_header_html",
    "_cost_coverage_bar_html",
    "_delta_pill",
    "_extent_dates",
    "_fan_out_reads",
    "_filter_meta_html",
    "_insights_html",
    "_issues_table_html",
    "_kpi_strip_html",
    "_parse_parallel_reads_flag",
    "_reliability_tiles_html",
    "_render_drilldown",
    "_skill_matrix_html",
    "_skill_triggers_html",
    "_sparkline_svg",
    "_topbar_html",
    "analytics",
    "analytics_read",
    "cache_key",
    "compute_insights",
    "dashboard_parallel_reads_enabled",
    "db_unconfigured_message",
    "default_date_range",
    "format_tz_offset",
    "kpi_delta",
    "main",
    "parse_issue_number",
    "parse_skill_matrix_sort",
    "preset_window",
    "previous_window",
    "reliability_tile_data",
    "resolve_stage_filter",
    "rework_totals",
    "shift_ts",
    "to_window",
    "top_expensive_issues",
]

log = logging.getLogger(__name__)

DEFAULT_RECENT_AGENT_EXITS = 100

# TTL for the data-extent / filter-option reads (`get_data_extent`,
# `get_filter_options`). These reads carry no filter inputs and
# change only as `analytics.sync` ingests fresh events, so they
# tolerate a longer TTL than the 60 s window the per-filter cached
# wrappers use. Five minutes keeps a freshly-synced repo / event
# value reachable within one sync cycle while collapsing the
# topbar / sidebar round-trip on every rerun.
STATIC_METADATA_TTL_SECONDS = 300

LOADING_INDICATOR_MESSAGE = "Loading analytics…"

# Plotly config passed to every `st.plotly_chart` call. Disabling
# the modebar keeps the hover camera/zoom/pan toolbar off the cards
# -- the standalone mock has no chart chrome, and the toolbar pops
# on hover for every chart on the page otherwise.
PLOTLY_CONFIG: dict[str, Any] = {"displayModeBar": False}

NO_DATA_MESSAGE = (
    "No analytics events have been recorded yet. Run "
    "`uv run python -m orchestrator.analytics.sync` after some "
    "workflow activity to populate the dashboard."
)
EMPTY_WINDOW_MESSAGE = (
    "No analytics events match the current filters. Broaden the window "
    "or clear a filter to see activity."
)


# Data-table sizing (px): per-row height plus fixed header/padding base.
_TABLE_ROW_HEIGHT = 40
_TABLE_BASE_HEIGHT = 80


def main() -> None:
    """Run the Streamlit analytics page with lazily loaded dependencies."""
    import streamlit as st

    _run_dashboard(st)


@dataclass(frozen=True)
class _DashboardModules:
    st: Any
    pd: Any
    charts: Any
    theme: Any


@dataclass(frozen=True)
class _SidebarSelections:
    repo: str
    events: Sequence[str]
    stages: Sequence[str]
    issue_input: str


@dataclass(frozen=True)
class _DashboardFilters:
    window: DateWindow
    repo: Optional[str]
    issue_input: Optional[int]
    events: Optional[Sequence[str]]
    stages: Optional[Sequence[str]]

    @property
    def issue(self) -> Optional[int]:
        if self.repo is None:
            return None
        return self.issue_input

    @property
    def days(self) -> int:
        return max((self.window.end - self.window.start).days, 1)


@dataclass(frozen=True)
class _DashboardControls:
    filters: _DashboardFilters
    topbar_slot: Any
    meta_slot: Any
    timezone_offset: int


@dataclass(frozen=True)
class _DashboardReadPlan:
    first_wave: Sequence[tuple[str, Callable[[], Any]]]
    second_wave: Sequence[tuple[str, Callable[[], Any]]]
    parallel: bool
    started_at: float

    @property
    def total_reads(self) -> int:
        return len(self.first_wave) + len(self.second_wave)


@dataclass(frozen=True)
class _DashboardPage:
    extent: DataExtent
    controls: _DashboardControls
    reads: _DashboardReadPlan


@dataclass(frozen=True)
class _DashboardKpis:
    tiles: Sequence[dict[str, Any]]
    resolved: int
    rejected: int


@dataclass(frozen=True)
class _LoadedDashboard:
    read_results: dict[str, Any]
    kpis: _DashboardKpis


@dataclass(frozen=True)
class _ReliabilityPanelData:
    repos: Sequence[Any]
    summary: Summary
    throughput: Sequence[Any]
    window: DateWindow
    resolved: int
    rejected: int


def _load_dashboard_modules(st: Any) -> _DashboardModules:
    import pandas as pd

    from orchestrator import dashboard_charts, dashboard_theme

    return _DashboardModules(
        st=st,
        pd=pd,
        charts=dashboard_charts,
        theme=dashboard_theme,
    )


def _configure_dashboard(modules: _DashboardModules) -> None:
    modules.st.set_page_config(
        page_title="Orchestrator Analytics",
        layout="wide",
    )
    modules.st.markdown(modules.theme.PAGE_CSS, unsafe_allow_html=True)


def _stop_if_dashboard_unconfigured(modules: _DashboardModules) -> None:
    message = db_unconfigured_message()
    if not message:
        return
    modules.st.warning(message)
    modules.st.stop()


def _run_dashboard(st: Any) -> None:
    modules = _load_dashboard_modules(st)
    _configure_dashboard(modules)
    _stop_if_dashboard_unconfigured(modules)
    _render_dashboard(
        modules,
        *_read_static_metadata(st=modules.st),
    )


def _render_dashboard(
    modules: _DashboardModules,
    extent: DataExtent,
    options: Any,
) -> None:
    if extent.min_ts is None or extent.max_ts is None:
        _render_no_data(st=modules.st, extent=extent, theme=modules.theme)
        return
    page = _prepare_dashboard_page(modules, extent, options)
    loaded = _load_dashboard_data(modules, page)
    if loaded is None:
        return
    _render_dashboard_widgets(modules, page, loaded)


def _timezone_choice(st: Any) -> int:
    if "tz_offset_hours" not in st.session_state:
        st.session_state.tz_offset_hours = DEFAULT_TZ_OFFSET_HOURS
    return int(st.session_state.tz_offset_hours)


def _resolve_dashboard_filters(
    window: DateWindow,
    selections: _SidebarSelections,
    options: Any,
) -> _DashboardFilters:
    repo = None
    if selections.repo != "All":
        repo = selections.repo
    return _DashboardFilters(
        window=window,
        repo=repo,
        issue_input=parse_issue_number(selections.issue_input),
        events=list(selections.events),
        stages=resolve_stage_filter(selections.stages, options.stages),
    )


def _render_dashboard_controls(
    modules: _DashboardModules,
    extent: DataExtent,
    options: Any,
) -> _DashboardControls:
    selections = _render_sidebar_filters(st=modules.st, options=options)
    timezone_offset = _timezone_choice(modules.st)
    topbar_slot = modules.st.empty()
    window_meta = _render_date_filter_bar(
        st=modules.st,
        extent=extent,
        extent_min_d=extent.min_ts.date(),
        extent_max_d=extent.max_ts.date(),
    )
    return _DashboardControls(
        filters=_resolve_dashboard_filters(window_meta[0], selections, options),
        topbar_slot=topbar_slot,
        meta_slot=window_meta[1],
        timezone_offset=timezone_offset,
    )


def _prepare_dashboard_page(
    modules: _DashboardModules,
    extent: DataExtent,
    options: Any,
) -> _DashboardPage:
    controls = _render_dashboard_controls(modules, extent, options)
    keys = _build_read_keys(
        window=controls.filters.window,
        repo_filter=controls.filters.repo,
        event_filter=controls.filters.events,
        stage_filter=controls.filters.stages,
        issue_filter=controls.filters.issue,
    )
    readers = _widget_readers(
        st=modules.st,
        key=keys[0],
        prev_key=keys[1],
        tz_offset_choice=controls.timezone_offset,
    )
    return _DashboardPage(
        extent=extent,
        controls=controls,
        reads=_DashboardReadPlan(
            first_wave=readers[0],
            second_wave=readers[1],
            parallel=dashboard_parallel_reads_enabled(),
            started_at=perf_counter(),
        ),
    )


def _render_topbar_and_meta(
    modules: _DashboardModules,
    page: _DashboardPage,
    summary: Summary,
) -> None:
    page.controls.topbar_slot.markdown(
        _topbar_html(
            extent=page.extent,
            distinct_repos=summary.distinct_repos,
            total_events=summary.total_events,
            spend_in_range=summary.total_cost_usd,
            fmt_money_exact=modules.theme.fmt_money_exact,
            fmt_num=modules.theme.fmt_num,
        ),
        unsafe_allow_html=True,
    )
    page.controls.meta_slot.markdown(
        _filter_meta_html(
            from_d=page.controls.filters.window.start.date(),
            to_d=(
                page.controls.filters.window.end - timedelta(days=1)
            ).date(),
            days=page.controls.filters.days,
            runs=summary.total_agent_runs,
            fmt_num=modules.theme.fmt_num,
        ),
        unsafe_allow_html=True,
    )


def _render_dashboard_insights(
    modules: _DashboardModules,
    summary: Summary,
    cost_coverage_rows: Sequence[CostCoverageRow],
) -> None:
    banners = compute_insights(
        summary,
        cost_coverage_rows=cost_coverage_rows,
    )
    if banners:
        modules.st.markdown(_insights_html(banners), unsafe_allow_html=True)


def _render_first_wave(
    modules: _DashboardModules,
    page: _DashboardPage,
    read_results: dict[str, Any],
) -> Optional[_DashboardKpis]:
    summary = read_results["summary"]
    _render_topbar_and_meta(modules, page, summary)
    if summary.total_events == 0:
        _render_empty_window(modules, page)
        return None
    _render_dashboard_insights(
        modules,
        summary,
        read_results["cost_coverage_rows"],
    )
    kpi_values = _build_kpi_strip_data(_KpiInputs(
        theme=modules.theme,
        summary=summary,
        prev_summary=read_results["prev_summary"],
        ts_points=read_results["ts_points"],
        throughput_rows=read_results["throughput_rows"],
        review_round_rows=read_results["review_round_rows"],
        days_in_window=page.controls.filters.days,
    ))
    modules.st.markdown(
        _kpi_strip_html(kpi_values[0]),
        unsafe_allow_html=True,
    )
    return _DashboardKpis(*kpi_values)


def _load_dashboard_data(
    modules: _DashboardModules,
    page: _DashboardPage,
) -> Optional[_LoadedDashboard]:
    with modules.st.spinner(LOADING_INDICATOR_MESSAGE):
        read_results = _dispatch_reads(
            page.reads.first_wave,
            st=modules.st,
            parallel=page.reads.parallel,
        )
        kpis = _render_first_wave(modules, page, read_results)
        if kpis is None:
            return None
        read_results.update(_dispatch_reads(
            page.reads.second_wave,
            st=modules.st,
            parallel=page.reads.parallel,
        ))
    _log_dashboard_load(
        load_start=page.reads.started_at,
        reads=page.reads.total_reads,
        parallel=page.reads.parallel,
    )
    return _LoadedDashboard(read_results=read_results, kpis=kpis)


def _render_chart_widgets(
    modules: _DashboardModules,
    page: _DashboardPage,
    loaded: _LoadedDashboard,
) -> None:
    _render_hero_usage(
        st=modules.st,
        dashboard_charts=modules.charts,
        ts_points=loaded.read_results["ts_points"],
        backend_daily_rows=loaded.read_results["backend_daily_rows"],
    )
    _render_stage_review_bars(
        st=modules.st,
        dashboard_charts=modules.charts,
        stage_rows=loaded.read_results["stage_rows"],
        review_round_rows=loaded.read_results["review_round_rows"],
    )
    _render_issues_and_backends(
        st=modules.st,
        theme=modules.theme,
        issues_rows=loaded.read_results["issues_rows"],
        backend_rows=loaded.read_results["backend_rows"],
        cost_coverage_rows=loaded.read_results["cost_coverage_rows"],
    )
    _render_repo_and_reliability(
        modules,
        _ReliabilityPanelData(
            repos=loaded.read_results["repo_rows"],
            summary=loaded.read_results["summary"],
            throughput=loaded.read_results["throughput_rows"],
            window=page.controls.filters.window,
            resolved=loaded.kpis.resolved,
            rejected=loaded.kpis.rejected,
        ),
    )
    _render_activity_heatmap(
        st=modules.st,
        dashboard_charts=modules.charts,
        heatmap_rows=loaded.read_results["heatmap_rows"],
        tz_offset_choice=page.controls.timezone_offset,
    )


def _render_remaining_widgets(
    modules: _DashboardModules,
    page: _DashboardPage,
    loaded: _LoadedDashboard,
) -> None:
    _render_skill_triggers(
        st=modules.st,
        skill_rows=loaded.read_results["skill_rows"],
        skill_matrix_rows=loaded.read_results["skill_matrix_rows"],
    )
    _render_recent_runs(
        st=modules.st,
        pd=modules.pd,
        agent_exits=loaded.read_results["agent_exits"],
        tz_offset_choice=page.controls.timezone_offset,
    )
    _render_drilldown_view(modules, page.controls.filters)
    _render_dashboard_footer(
        modules,
        page.controls.filters,
        loaded.read_results["summary"],
    )


def _render_dashboard_widgets(
    modules: _DashboardModules,
    page: _DashboardPage,
    loaded: _LoadedDashboard,
) -> None:
    _render_chart_widgets(modules, page, loaded)
    _render_remaining_widgets(modules, page, loaded)


def _render_dashboard_footer(
    modules: _DashboardModules,
    filters: _DashboardFilters,
    summary: Summary,
) -> None:
    end_date = (filters.window.end - timedelta(days=1)).date()
    window_start = filters.window.start.date().isoformat()
    agent_runs = modules.theme.fmt_num(summary.total_agent_runs)
    modules.st.markdown(
        '<div class="orch-foot">'
        f'Real data · window {window_start} → '
        f'{end_date.isoformat()} · '
        f'{agent_runs} agent runs'
        '</div>',
        unsafe_allow_html=True,
    )
def _filter_list(values_t: Optional[Sequence[str]]) -> Optional[list[str]]:
    """Convert a cached filter tuple back to the read model's list arg.

    `cache_key` stores the event / stage multiselects as hashable
    tuples so they can key `st.cache_data`; the `analytics.read`
    getters take lists. Converting per read keeps the tri-state intact
    -- `None` means "no filter", an empty selection means "show
    nothing", and the two must stay distinct at the read layer.
    """
    if values_t is None:
        return None
    return list(values_t)


def _scoped_read(getter: Callable[..., Any], /, **filters: Any) -> Any:
    """Run one windowed read on the per-thread analytics connection.

    Checks out the thread-local connection via `analytics_connection()`
    and forwards it to `getter` alongside the resolved filter kwargs, so
    every cached reader shares one open socket per render pass instead of
    opening (and hashing) a connection per call. The cached wrappers stay
    connection-free: `conn` is supplied here and never lands in their
    `st.cache_data` key (a raw `psycopg.Connection` is unhashable and
    would make every reload look like a cache miss).
    """
    with analytics_read.analytics_connection() as conn:
        return getter(conn=conn, **filters)


def _read_data_extent():
    return _scoped_read(analytics_read.get_data_extent)


def _read_filter_options():
    return _scoped_read(analytics_read.get_filter_options)


def _read_static_metadata(*, st: Any):
    """Read the data extent + filter options through cached wrappers.

    `get_data_extent` / `get_filter_options` carry no filter inputs (the
    cache key is empty) and only change as `analytics.sync` ingests new
    events, so both are cached under the longer `STATIC_METADATA_TTL_SECONDS`
    (5 min) rather than the per-filter 60 s TTL -- collapsing the sidebar /
    topbar round-trip on every rerun. Returns `(extent, options)`; a read
    error is surfaced as one `st.error` and stops the app.
    """
    read_data_extent = st.cache_data(
        show_spinner=False, ttl=STATIC_METADATA_TTL_SECONDS,
    )(_read_data_extent)
    read_filter_options = st.cache_data(
        show_spinner=False, ttl=STATIC_METADATA_TTL_SECONDS,
    )(_read_filter_options)

    try:
        return read_data_extent(), read_filter_options()
    except analytics_read.AnalyticsReadError as error:
        st.error(
            "Could not load analytics filter options: "
            f"{error}. Verify `ANALYTICS_DB_URL` and that the Postgres "
            "service is reachable, then reload."
        )
        st.stop()


def _render_no_data(*, st: Any, extent: DataExtent, theme: Any) -> None:
    """Render the no-data startup state and stop.

    The data extent is empty (`analytics_events` holds zero rows), so paint
    the topbar with zeroed counts and surface `NO_DATA_MESSAGE` below it
    before halting the app.
    """
    st.markdown(
        _topbar_html(
            extent=extent,
            distinct_repos=0,
            total_events=0,
            spend_in_range=0.0,
            fmt_money_exact=theme.fmt_money_exact,
            fmt_num=theme.fmt_num,
        ),
        unsafe_allow_html=True,
    )
    st.info(NO_DATA_MESSAGE)
    st.stop()


def _read_filter_kwargs(key: tuple) -> dict[str, Any]:
    return {
        "start": key[0],
        "end": key[1],
        "repo": key[2],
        "events": _filter_list(key[3]),
        "stages": _filter_list(key[4]),
        "issue": key[5],
    }


def _read_filtered(
    getter: Callable[..., Any],
    key: tuple,
    **extra_filters: Any,
) -> Any:
    filters = _read_filter_kwargs(key)
    filters.update(extra_filters)
    return _scoped_read(getter, **filters)


def _read_summary(key: tuple):
    return _read_filtered(analytics_read.get_summary, key)


def _read_prev_kpi(key: tuple):
    # Previous-window read for the KPI delta pills and cost-trend
    # banner only. The full `get_summary` shape is never read off
    # `prev_summary`, so a thinner reader saves a `GROUP BY` follow-up
    # while leaving the cache key identical to `_read_summary`.
    return _read_filtered(analytics_read.get_kpi_prev, key)


def _read_time_series(key: tuple):
    return _read_filtered(analytics_read.get_time_series, key)


def _read_stage_breakdown(key: tuple):
    return _read_filtered(analytics_read.get_stage_breakdown, key)


def _read_recent_agent_exits(key: tuple):
    return _read_filtered(
        analytics_read.get_recent_agent_exits,
        key,
        limit=DEFAULT_RECENT_AGENT_EXITS,
    )


def _read_top_cost_issues(key: tuple):
    # Ask the database for the top-cost issues directly. Reading the
    # latest N issues by `last_seen` and re-sorting in Python silently
    # drops older high-cost issues that fall outside the truncated set.
    return _read_filtered(
        analytics_read.get_issues,
        key,
        limit=DEFAULT_EXPENSIVE_LIMIT,
        sort_by=analytics_read.SORT_BY_COST,
    )


def _read_review_round(key: tuple):
    return _read_filtered(analytics_read.get_review_round_breakdown, key)


def _read_backend_efficiency(key: tuple):
    return _read_filtered(analytics_read.get_backend_efficiency, key)


def _read_repo_breakdown(key: tuple):
    return _read_filtered(analytics_read.get_repo_breakdown, key)


def _read_cost_coverage(key: tuple):
    return _read_filtered(analytics_read.get_cost_coverage, key)


def _read_hourly_heatmap(
    key: tuple,
    tz_offset_hours: int,
):
    return _read_filtered(
        analytics_read.get_hourly_heatmap,
        key,
        tz_offset_hours=tz_offset_hours,
    )


def _read_throughput(key: tuple):
    return _read_filtered(analytics_read.get_throughput_breakdown, key)


def _read_backend_daily_tokens(key: tuple):
    return _read_filtered(analytics_read.get_backend_daily_tokens, key)


def _read_skill_trigger_rates(key: tuple):
    return _read_filtered(analytics_read.get_skill_trigger_rates, key)


def _read_skill_trigger_matrix(key: tuple):
    return _read_filtered(analytics_read.get_skill_trigger_matrix, key)


def _widget_task(
    st: Any,
    name: str,
    reader: Callable[..., Any],
    *args: Any,
) -> tuple[str, Callable[[], Any]]:
    cached_reader = st.cache_data(show_spinner=False, ttl=60)(reader)
    return name, partial(cached_reader, *args)


def _first_wave_readers(
    st: Any,
    key: tuple,
    prev_key: tuple,
) -> list[tuple[str, Callable[[], Any]]]:
    return [
        _widget_task(st, "summary", _read_summary, key),
        _widget_task(st, "prev_summary", _read_prev_kpi, prev_key),
        _widget_task(st, "ts_points", _read_time_series, key),
        _widget_task(st, "review_round_rows", _read_review_round, key),
        _widget_task(st, "throughput_rows", _read_throughput, key),
        _widget_task(st, "cost_coverage_rows", _read_cost_coverage, key),
    ]


def _second_wave_readers(
    st: Any,
    key: tuple,
    tz_offset_choice: int,
) -> list[tuple[str, Callable[[], Any]]]:
    return [
        _widget_task(st, "stage_rows", _read_stage_breakdown, key),
        _widget_task(st, "agent_exits", _read_recent_agent_exits, key),
        _widget_task(st, "issues_rows", _read_top_cost_issues, key),
        _widget_task(st, "backend_rows", _read_backend_efficiency, key),
        _widget_task(st, "repo_rows", _read_repo_breakdown, key),
        _widget_task(
            st,
            "heatmap_rows",
            _read_hourly_heatmap,
            key,
            int(tz_offset_choice),
        ),
        _widget_task(st, "backend_daily_rows", _read_backend_daily_tokens, key),
        _widget_task(st, "skill_rows", _read_skill_trigger_rates, key),
        _widget_task(st, "skill_matrix_rows", _read_skill_trigger_matrix, key),
    ]


def _widget_readers(*, st: Any, key, prev_key, tz_offset_choice: int):
    """Define the cached per-filter read wrappers and stage them.

    Returns `(first_wave_readers, second_wave_readers)` -- each a list of
    `(name, zero-arg callable)` pairs `_fan_out_reads` dispatches.

    Connection scoping: each wrapper delegates through `_read_filtered`
    to `_scoped_read`, which checks out the thread-local connection via
    `analytics_connection()` and forwards it to the read helper rather
    than threading a connection through the cache key (a raw
    `psycopg.Connection` is not hashable and would crash the wrapper, and
    every reload would otherwise look like a cache miss). The thread-local
    persists across reads in the same render pass, so the first cache-miss
    pays the psycopg handshake and the rest reuse the open socket. The
    cache key stays the filter tuple `(start, end, repo, events_t,
    stages_t, issue)`.

    Split into two staged waves so the topbar / filter meta / insight
    banners / KPI strip can paint as soon as their inputs are available
    instead of blocking on every widget: the first wave carries the six
    reads those above-the-fold widgets consume, the second the nine
    remaining widget reads. Each task is a zero-argument `partial` bound
    to its immutable filter tuple, and worker threads only return data --
    every `st.*` write happens on the caller's render thread between waves.
    """
    return (
        _first_wave_readers(st, key, prev_key),
        _second_wave_readers(st, key, tz_offset_choice),
    )


def _dispatch_reads(readers, *, st: Any, parallel: bool):
    """Dispatch one read wave and surface a read error as one banner.

    Runs the wave through `_fan_out_reads` (sequential, or across a thread
    pool when `parallel`) and returns the name->data dict. An
    `AnalyticsReadError` from any reader is caught, rendered as one
    `st.error`, and stops the app -- the dashboard cannot render without
    database access.
    """
    try:
        return _fan_out_reads(readers, parallel=parallel)
    except analytics_read.AnalyticsReadError as error:
        st.error(
            f"Analytics query failed: {error}. The dashboard cannot render "
            "without database access; check Postgres connectivity and "
            "reload."
        )
        st.stop()


def _log_dashboard_load(*, load_start: float, reads: int, parallel: bool) -> None:
    """Emit the single `dashboard.load:` INFO line for the A/B rollout.

    Carries total wall-clock, the reader count (6 when the empty-window
    short-circuit skips the second wave, else 15), and the parallel flag,
    so the sequential / parallel paths can be A/B'd with one
    `grep dashboard.load streamlit.log`.
    """
    log.info(
        "dashboard.load: total=%.1fs reads=%d parallel=%s",
        perf_counter() - load_start,
        reads,
        "true" if parallel else "false",
    )


def _render_empty_window(
    modules: _DashboardModules,
    page: _DashboardPage,
) -> None:
    """Render the empty-window state (no events match the filters).

    The first wave's summary returned zero events, so the second wave is
    skipped entirely -- the remaining widget reads would only paint empty
    cards. Logs the short-circuit (so the A/B line still lands), surfaces
    `EMPTY_WINDOW_MESSAGE`, and still renders the per-issue drill-down
    (which runs its own read).
    """
    _log_dashboard_load(
        load_start=page.reads.started_at,
        reads=len(page.reads.first_wave),
        parallel=page.reads.parallel,
    )
    modules.st.info(EMPTY_WINDOW_MESSAGE)
    _render_drilldown_view(modules, page.controls.filters)


def _render_sidebar_filters(*, st: Any, options: Any):
    """Render the sidebar filter widgets; return the raw selections.

    The repo selector plus the event / stage multiselects and the
    issue-number input. Returns `(repo_choice, event_choice,
    stage_choice, issue_input)`; the caller resolves these into the
    tri-state read filters. An empty multiselect is a deliberate "show
    nothing for these" signal, not "no filter" -- that distinction is
    made downstream, not here.
    """
    with st.sidebar:
        st.header("Filters")
        repo_options = ("All", *options.repos) if options.repos else ("All",)
        repo_choice = st.selectbox("Repo", repo_options, index=0)
        event_choice = st.multiselect(
            "Events",
            list(options.events),
            default=list(options.events),
            help=(
                "Narrows every widget. An empty selection means "
                "'show nothing for these events'."
            ),
        )
        stage_choice = st.multiselect(
            "Stages",
            list(options.stages),
            default=list(options.stages),
            help=(
                "Narrows every widget. An empty selection means "
                "'show nothing for these stages'."
            ),
        )
        issue_input = st.text_input(
            "Issue number",
            value="",
            help=(
                "Enter `123` or `#123` to narrow every widget to one "
                "issue AND render the per-issue event trace at the "
                "bottom. Requires a specific repo above."
            ),
        )
    return _SidebarSelections(
        repo=repo_choice,
        events=event_choice,
        stages=stage_choice,
        issue_input=issue_input,
    )


@dataclass(frozen=True)
class _DateFilterColumns:
    label: Any
    preset: Any
    start: Any
    end: Any
    meta: Any


def _date_filter_columns(st: Any) -> _DateFilterColumns:
    columns = st.columns(
        [1.0, 1.7, 1.4, 1.4, 3.0],
        vertical_alignment="bottom",
    )
    return _DateFilterColumns(*columns)


def _render_date_filter_label(st: Any, column: Any) -> None:
    with column:
        st.markdown(
            '<div class="orch-filterbar-anchor"></div>'
            '<span class="orch-filter-label">Date range</span>',
            unsafe_allow_html=True,
        )


def _preset_radio_index(preset: str) -> int:
    choices = (PRESET_3D, PRESET_7D, PRESET_ALL)
    if preset not in choices:
        return 2
    return choices.index(preset)


def _render_preset_choice(st: Any, column: Any) -> str:
    with column:
        return st.radio(
            "Range preset",
            options=(PRESET_3D, PRESET_7D, PRESET_ALL),
            format_func=lambda preset: PRESET_INLINE_LABELS[preset],
            index=_preset_radio_index(st.session_state.preset),
            horizontal=True,
            label_visibility="collapsed",
            key="_preset_radio",
        )


def _initial_filter_window(
    preset_choice: str,
    extent: DataExtent,
    extent_min_d: date,
    extent_max_d: date,
) -> DateWindow:
    return (
        preset_window(preset_choice, extent)
        or to_window(extent_min_d, extent_max_d)
    )


def _render_date_inputs(
    st: Any,
    columns: _DateFilterColumns,
    initial_window: DateWindow,
    extent_min_d: date,
    extent_max_d: date,
) -> tuple[date, date]:
    with columns.start:
        start_date = st.date_input(
            "From",
            value=initial_window.start.date(),
            min_value=extent_min_d,
            max_value=extent_max_d,
        )
    with columns.end:
        end_date = st.date_input(
            "To",
            value=(initial_window.end - timedelta(days=1)).date(),
            min_value=extent_min_d,
            max_value=extent_max_d,
        )
    return start_date, end_date


def _render_date_filter_bar(
    *,
    st: Any,
    extent: DataExtent,
    extent_min_d: date,
    extent_max_d: date,
):
    """Render the preset + date-range filter bar.

    Returns `(window, meta_slot)`: the resolved `DateWindow` every
    downstream read is scoped to, and the `st.empty()` placeholder the
    range meta ("… → … · N days · N runs") fills once the summary read
    lands (the run count is not known yet). The selected preset persists
    in `st.session_state` so a custom pick survives a rerun.
    """
    if "preset" not in st.session_state:
        st.session_state.preset = DEFAULT_PRESET
    with st.container(border=True):
        # A hidden `.orch-cardmark` as the bordered container's first
        # child lets the shared white-card rule in
        # `dashboard_theme.PAGE_CSS` (`:has(> stElementContainer
        # .orch-cardmark)`) paint this filter bar like every other card --
        # Streamlit 1.58 dropped the stable border-wrapper testid the old
        # per-card selector relied on. The `.orch-filterbar-anchor` below
        # stays in the left column purely as the hidden label sentinel.
        st.markdown(
            '<div class="orch-cardmark"></div>', unsafe_allow_html=True
        )
        columns = _date_filter_columns(st)
        _render_date_filter_label(st, columns.label)
        preset_choice = _render_preset_choice(st, columns.preset)
        initial_window = _initial_filter_window(
            preset_choice, extent, extent_min_d, extent_max_d,
        )
        dates = _render_date_inputs(
            st, columns, initial_window, extent_min_d, extent_max_d,
        )
        # The run count is not known until the summary read lands, so
        # capture the meta slot now and fill it between fan-out waves.
        with columns.meta:
            meta_slot = st.empty()
    st.session_state.preset = preset_choice
    return to_window(*dates), meta_slot


def _build_read_keys(
    *,
    window: DateWindow,
    repo_filter: Optional[str],
    event_filter: Optional[Sequence[str]],
    stage_filter: Optional[Sequence[str]],
    issue_filter: Optional[int],
):
    """Build the current + previous-window cache-key tuples.

    Returns `(key, prev_key)`: the `(start, end, repo, events, stages,
    issue)` tuple the fan-out reads are cached under, and the same
    tuple shifted to the immediately-preceding equal-length window for
    the KPI delta pills. Cached readers accept the tuple as one hashable
    key and `_read_filter_kwargs` expands its stable field order for the
    read model.
    """
    key = cache_key(
        window, repo_filter, event_filter, stage_filter, issue_filter
    )
    prev_key = cache_key(
        previous_window(window),
        repo_filter,
        event_filter,
        stage_filter,
        issue_filter,
    )
    return key, prev_key


def _summary_total_tokens(summary: Summary) -> int:
    """Return the dashboard token total used by KPIs and sparklines.

    Cache read/write tokens are counted with input/output so the KPI
    total matches the hero chart's Cache band. The cumulative
    `cached_tokens` field is intentionally excluded to avoid double
    counting reused prompt slices.
    """
    return int(
        (summary.total_input_tokens or 0)
        + (summary.total_output_tokens or 0)
        + (summary.total_cache_read_tokens or 0)
        + (summary.total_cache_write_tokens or 0)
    )


def _time_series_total_tokens(point: Any) -> float:
    return float(
        (point.input_tokens or 0)
        + (point.output_tokens or 0)
        + (point.cache_read_tokens or 0)
        + (point.cache_write_tokens or 0)
    )


def _throughput_totals(throughput_rows: Sequence[Any]) -> tuple[int, int]:
    resolved = sum(int(row.resolved or 0) for row in throughput_rows)
    rejected = sum(int(row.rejected or 0) for row in throughput_rows)
    return resolved, rejected


def _daily_point_totals(
    ts_points: Sequence[Any],
) -> dict[date, list[float]]:
    totals: dict[date, list[float]] = {}
    for point in ts_points:
        daily = totals.setdefault(point.day, [0.0, 0.0])
        daily[0] += float(point.cost_usd or 0)
        daily[1] += _time_series_total_tokens(point)
    return totals


@dataclass(frozen=True)
class _DailyKpiSeries:
    cost: Sequence[float]
    tokens: Sequence[float]
    done: Sequence[int]


def _daily_kpi_series(
    *, ts_points: Sequence[Any], throughput_rows: Sequence[Any]
) -> _DailyKpiSeries:
    """Return cost/token/resolved sparkline series for KPI cards.

    One entry is emitted per day present in the time-series read. Daily
    tokens use the same input + output + cache_read + cache_write
    accounting as the headline token KPI.
    """
    totals = _daily_point_totals(ts_points)
    days = sorted(totals)
    done_index = {row.day: int(row.resolved or 0) for row in throughput_rows}
    return _DailyKpiSeries(
        cost=[totals[day][0] for day in days],
        tokens=[totals[day][1] for day in days],
        done=[done_index.get(day, 0) for day in days],
    )


@dataclass(frozen=True)
class _KpiInputs:
    theme: Any
    summary: Summary
    prev_summary: Summary
    ts_points: Sequence[Any]
    throughput_rows: Sequence[Any]
    review_round_rows: Sequence[Any]
    days_in_window: int


@dataclass(frozen=True)
class _KpiTotals:
    cost: float
    tokens: int
    previous_cost: float
    previous_tokens: int
    resolved: int
    rejected: int
    review_cost: float
    rework_cost: float


def _kpi_totals(inputs: _KpiInputs) -> _KpiTotals:
    throughput = _throughput_totals(inputs.throughput_rows)
    review_costs = rework_totals(inputs.review_round_rows)
    return _KpiTotals(
        cost=float(inputs.summary.total_cost_usd or 0),
        tokens=_summary_total_tokens(inputs.summary),
        previous_cost=float(inputs.prev_summary.total_cost_usd or 0),
        previous_tokens=_summary_total_tokens(inputs.prev_summary),
        resolved=throughput[0],
        rejected=throughput[1],
        review_cost=review_costs[0],
        rework_cost=review_costs[1],
    )


def _cost_per_resolved(totals: _KpiTotals) -> str:
    if totals.resolved <= 0:
        return "—"
    avg_cost = totals.cost / totals.resolved
    return f"${avg_cost:,.2f}"


def _kpi_strip_entries(
    inputs: _KpiInputs,
    totals: _KpiTotals,
    daily: _DailyKpiSeries,
    rework_share: float,
) -> list[dict[str, Any]]:
    daily_cost = inputs.theme.fmt_money(totals.cost / inputs.days_in_window)
    daily_tokens = inputs.theme.fmt_tokens(totals.tokens / inputs.days_in_window)
    rework_pct = rework_share * 100
    rework_cost = inputs.theme.fmt_money_exact(totals.rework_cost)
    return [
        {
            "label": "Total spend",
            "value": inputs.theme.fmt_money_exact(totals.cost),
            "delta": kpi_delta(totals.cost, totals.previous_cost),
            "sub": f"{daily_cost}/day",
            "spark": daily.cost,
            "spark_color": inputs.theme.ACCENT,
        },
        {
            "label": "Total tokens",
            "value": inputs.theme.fmt_tokens(totals.tokens),
            "delta": kpi_delta(totals.tokens, totals.previous_tokens),
            "sub": f"{daily_tokens}/day",
            "spark": daily.tokens,
            "spark_color": inputs.theme.TOKEN_TYPE_COLORS["Input"],
        },
        {
            "label": "Cost / resolved issue",
            "value": _cost_per_resolved(totals),
            "delta": None,
            "sub": f"{totals.resolved} resolved · {totals.rejected} rejected",
            "spark": daily.done,
            "spark_color": inputs.theme.TOKEN_TYPE_COLORS["Cache"],
        },
        {
            "label": "Rework share",
            "value": f"{rework_pct:.0f}%",
            "delta": None,
            "sub": f"{rework_cost} in review rounds >= 1",
            "spark": None,
        },
    ]


def _build_kpi_strip_data(
    inputs: _KpiInputs,
) -> tuple[list[dict[str, Any]], int, int]:
    """Build the KPI-strip dictionaries plus throughput totals."""
    totals = _kpi_totals(inputs)
    rework_share = (
        (totals.rework_cost / totals.review_cost)
        if totals.review_cost > 0
        else 0.0
    )
    daily = _daily_kpi_series(
        ts_points=inputs.ts_points,
        throughput_rows=inputs.throughput_rows,
    )
    kpis = _kpi_strip_entries(inputs, totals, daily, rework_share)
    return kpis, totals.resolved, totals.rejected


def _backend_tokens_by_day(
    backend_daily_rows: Sequence[Any],
) -> dict[date, dict[str, float]]:
    backend_by_day: dict[date, dict[str, float]] = {}
    for row in backend_daily_rows:
        by_backend = backend_by_day.setdefault(row.day, {})
        by_backend[row.backend] = (
            by_backend.get(row.backend, 0)
            + float(row.total_tokens or 0)
        )
    return backend_by_day


def _stack_mode_label(mode: str) -> str:
    if mode == "type":
        return "By token type"
    return "By backend"


def _stack_mode_index(mode: str) -> int:
    if mode == "type":
        return 0
    return 1


def _render_hero_usage(
    *,
    st: Any,
    dashboard_charts: Any,
    ts_points: Any,
    backend_daily_rows: Any,
) -> None:
    """Render the hero spend / token-usage stacked-area card.

    Carries the "By token type / By backend" toggle. The backend stack
    is built off `get_backend_daily_tokens` (not the LIMIT-capped
    recent-runs table) so a busy window's stack matches the full-window
    cost line and KPI tiles instead of silently undercounting.
    """
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "Spend & token usage over time",
                "Daily token consumption with cost trend overlaid",
            ),
            unsafe_allow_html=True,
        )
        if "stack_mode" not in st.session_state:
            st.session_state.stack_mode = "type"
        stack_mode = st.radio(
            "Stack mode",
            options=("type", "backend"),
            format_func=_stack_mode_label,
            index=_stack_mode_index(st.session_state.stack_mode),
            horizontal=True,
            label_visibility="collapsed",
            key="_stack_mode_radio",
        )
        st.session_state.stack_mode = stack_mode

        backend_by_day = (
            _backend_tokens_by_day(backend_daily_rows)
            if stack_mode == "backend" else None
        )

        st.plotly_chart(
            dashboard_charts.usage_over_time(
                ts_points,
                backend_rows_by_day=backend_by_day,
                mode=stack_mode,
                # The card header already renders the title; suppress
                # the in-chart title so it is not duplicated.
                title=None,
            ),
            use_container_width=True,
            config=PLOTLY_CONFIG,
        )


def _render_stage_review_bars(
    *,
    st: Any,
    dashboard_charts: Any,
    stage_rows: Any,
    review_round_rows: Any,
) -> None:
    """Render the side-by-side per-stage / per-review-round cost bars.

    Both bar panels are pinned to the same height (driven by whichever
    has more bars) so the two cards line up bottom-to-bottom.
    """
    bars_h = _paired_bars_height(stage_rows, review_round_rows)
    col_stage, col_round = st.columns([7, 5])
    with col_stage:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Cost by workflow stage",
                    "Where spend lands across the issue lifecycle",
                ),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.cost_by_stage(stage_rows, height=bars_h),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )
    with col_round:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Development and review by round",
                    "Developer and reviewer spend per review cycle",
                ),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.cost_by_review_round(
                    review_round_rows, height=bars_h
                ),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )


def _paired_bars_height(stage_rows: Sequence[Any], review_rows: Sequence[Any]) -> int:
    row_count = max(len(stage_rows), len(review_rows), 1)
    return _TABLE_ROW_HEIGHT * row_count + _TABLE_BASE_HEIGHT


def _render_issues_and_backends(
    *,
    st: Any,
    theme: Any,
    issues_rows: Any,
    backend_rows: Any,
    cost_coverage_rows: Any,
) -> None:
    """Render the top-cost issues table + backend-efficiency column.

    Left (7/12): the "Most expensive issues" table. `issues_rows` is
    already cost-ordered from SQL, but it is piped through
    `top_expensive_issues` so the in-memory cost / event-count
    tie-breakers stay authoritative and the set never exceeds
    `DEFAULT_EXPENSIVE_LIMIT`. Right (5/12): the per-backend efficiency
    cards above the cost-source coverage bar.
    """
    col_issues, col_backend = st.columns([7, 5])
    with col_issues:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Most expensive issues",
                    "Cost, run count, review rounds, and failure count",
                ),
                unsafe_allow_html=True,
            )
            expensive = top_expensive_issues(issues_rows)
            if expensive:
                st.markdown(
                    _issues_table_html(expensive),
                    unsafe_allow_html=True,
                )
            else:
                st.info("No agent runs with recorded cost in this window.")

    with col_backend:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Backend efficiency",
                    "Cost density, cache leverage, $/run",
                ),
                unsafe_allow_html=True,
            )
            if backend_rows:
                # One `st.markdown` per card (not a single joined
                # markdown) so Streamlit's inter-element gap keeps the
                # cards visually separated.
                for row in backend_rows:
                    st.markdown(
                        _backend_efficiency_card_html(row, theme=theme),
                        unsafe_allow_html=True,
                    )
            else:
                st.info("No `agent_exit` rows match the current filters.")
            if cost_coverage_rows:
                st.markdown(
                    _cost_coverage_bar_html(cost_coverage_rows, theme=theme),
                    unsafe_allow_html=True,
                )


def _render_repo_and_reliability(
    modules: _DashboardModules,
    panel: _ReliabilityPanelData,
) -> None:
    """Render the per-repo cost bars + reliability / throughput column.

    The reliability tiles source every value from the same full-window
    `Summary` aggregate (not the LIMIT-capped recent-runs read) so a
    long window still sees every timeout / failure. The resolved-per-day
    chart is passed the window so zero-resolution days the SQL elides
    still render an explicit bar against the calendar baseline.
    """
    col_repo, col_rel = modules.st.columns([7, 5])
    with col_repo:
        with modules.st.container(border=True):
            modules.st.markdown(
                _card_header_html(
                    "Cost by repository",
                    "Spend across managed repos",
                ),
                unsafe_allow_html=True,
            )
            modules.st.plotly_chart(
                modules.charts.cost_by_repo(panel.repos),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )
    with col_rel:
        with modules.st.container(border=True):
            modules.st.markdown(
                _card_header_html(
                    "Reliability & throughput",
                    "Run health and issues resolved per day",
                ),
                unsafe_allow_html=True,
            )
            raw_tiles = reliability_tile_data(
                panel.summary,
                resolved=panel.resolved,
                rejected=panel.rejected,
            )
            modules.st.markdown(
                _reliability_tiles_html(
                    raw_tiles,
                    fmt_num=modules.theme.fmt_num,
                ),
                unsafe_allow_html=True,
            )
            modules.st.plotly_chart(
                modules.charts.done_per_day_bars(
                    panel.throughput,
                    window_start=panel.window.start.date(),
                    window_end=(
                        panel.window.end - timedelta(days=1)
                    ).date(),
                    title=None,
                ),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )


def _render_activity_heatmap(
    *,
    st: Any,
    dashboard_charts: Any,
    heatmap_rows: Any,
    tz_offset_choice: int,
) -> None:
    """Render the weekday × hour token-volume heatmap card.

    The in-card UTC-offset selectbox binds to
    `st.session_state["tz_offset_hours"]` (seeded with the other controls
    before the second-wave fan-out) so the heatmap read and this widget
    agree on the offset; on change Streamlit reruns and the next read
    buckets in the newly-picked zone.
    """
    tz_label = format_tz_offset(int(tz_offset_choice))
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "When agents run",
                f"Token volume by hour ({tz_label}) × weekday",
            ),
            unsafe_allow_html=True,
        )
        st.selectbox(
            "Timezone",
            TZ_OFFSET_OPTIONS,
            key="tz_offset_hours",
            format_func=format_tz_offset,
            help=(
                "Shifts heatmap bucketing and the \"Recent agent "
                "runs\" `ts` column to the selected UTC offset. "
                "`ts` is stored in UTC."
            ),
        )
        st.plotly_chart(
            dashboard_charts.hour_weekday_heatmap(
                heatmap_rows, tz_label=tz_label,
            ),
            use_container_width=True,
            config=PLOTLY_CONFIG,
        )


def _render_skill_triggers(
    *,
    st: Any,
    skill_rows: Sequence[SkillTriggerRateRow],
    skill_matrix_rows: Sequence[SkillTriggerMatrixRow],
) -> None:
    """Render the skill-trigger aggregate table and matrix expander.

    The aggregate is an opt-in read-side widget over the
    `skills_triggered` / `skills_triggered_count` fields
    `record_agent_exit` folds into `extras` when
    `TRACK_SKILL_TRIGGERS` is on. A `0%` rate is a real signal ("this
    role's skill is not firing"), but it cannot tell a tracked-but-quiet
    run from one whose tracking was off, so the caption names the switch
    when nothing has triggered yet.
    """
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "Skill trigger rates",
                "Share of agent runs that triggered a skill, by role and "
                "backend (requires TRACK_SKILL_TRIGGERS)",
            ),
            unsafe_allow_html=True,
        )
        if not skill_rows:
            st.info("No `agent_exit` rows match the current filters.")
            return

        st.markdown(
            _skill_triggers_html(skill_rows),
            unsafe_allow_html=True,
        )
        if not any(row.skill_runs for row in skill_rows):
            st.caption(
                "No skill triggers recorded in this window. Enable "
                "`TRACK_SKILL_TRIGGERS` (default off) so "
                "`record_agent_exit` records which skills each run pulls."
            )
        _render_skill_matrix_expander(
            st=st,
            skill_matrix_rows=skill_matrix_rows,
        )


def _render_skill_matrix_expander(
    *,
    st: Any,
    skill_matrix_rows: Sequence[SkillTriggerMatrixRow],
) -> None:
    """Render the per-skill trigger matrix inside a collapsed expander."""
    with st.expander(
        "Per-skill trigger matrix · which skills each "
        "repo × role × backend cohort reaches for",
        expanded=False,
    ):
        # Clickable column headers re-sort the matrix: each header anchor
        # writes `mtx_sort` / `mtx_dir` query params, which this parses
        # back into a (column, direction) pair the render applies on top
        # of the read model's default order.
        matrix_sort_key, matrix_sort_desc = parse_skill_matrix_sort(
            st.query_params
        )
        st.markdown(
            _skill_matrix_html(
                skill_matrix_rows,
                sort_key=matrix_sort_key,
                descending=matrix_sort_desc,
            ),
            unsafe_allow_html=True,
        )


def _render_recent_runs(
    *,
    st: Any,
    pd: Any,
    agent_exits: Any,
    tz_offset_choice: int,
) -> None:
    """Render the "Recent agent runs" collapsible table.

    The `ts` column is shifted from stored UTC to the wall-clock of the
    selected offset via `shift_ts` so it reads in the same zone as the
    heatmap above it.
    """
    with st.expander("Recent agent runs", expanded=False):
        if agent_exits:
            ts_offset = timedelta(hours=int(tz_offset_choice))
            df_exits = pd.DataFrame([
                {
                    "ts": shift_ts(exit_row.ts, ts_offset),
                    "repo": exit_row.repo,
                    "issue": exit_row.issue,
                    "stage": exit_row.stage,
                    "agent": exit_row.agent_role,
                    "backend": exit_row.backend,
                    "duration (s)": exit_row.duration_s,
                    "exit": exit_row.exit_code,
                    "timed out": exit_row.timed_out,
                    "round": exit_row.review_round,
                    "retry": exit_row.retry_count,
                    "input tokens": exit_row.input_tokens,
                    "output tokens": exit_row.output_tokens,
                    "cost (USD)": exit_row.cost_usd,
                    "cost source": exit_row.cost_source,
                }
                for exit_row in agent_exits
            ])
            st.dataframe(df_exits, use_container_width=True)
        else:
            st.info("No `agent_exit` rows match the current filters.")


def _render_drilldown_view(
    modules: _DashboardModules,
    filters: _DashboardFilters,
) -> None:
    """Per-issue event trace section.

    Renders only when the operator typed a parseable issue number;
    when a repo is not also selected, surfaces an instructive notice
    so the empty result is not confused for a bug. Failures from the
    read model are caught and surfaced inline -- a drill-down error
    must not poison the overview the operator already scrolled past.
    """
    if filters.issue_input is None:
        return
    modules.st.subheader(f"Issue #{filters.issue_input} drill-down")
    if filters.repo is None:
        modules.st.info(
            "Pick a specific repo in the sidebar before drilling "
            "into an issue number -- GitHub issue numbers repeat "
            "across repos."
        )
        return
    try:
        trace = _scoped_read(
            analytics_read.get_issue_events,
            repo=filters.repo,
            issue=filters.issue_input,
            start=filters.window.start,
            end=filters.window.end,
            events=_filter_list(filters.events),
            stages=_filter_list(filters.stages),
        )
    except analytics_read.AnalyticsReadError as error:
        modules.st.error(f"Issue drill-down failed: {error}")
        return
    if trace:
        modules.st.dataframe(
            modules.pd.DataFrame([
                {
                    "ts": ev.ts,
                    "event": ev.event,
                    "stage": ev.stage,
                    "duration (s)": ev.duration_s,
                    "result": ev.result,
                    "agent": ev.agent_role,
                    "backend": ev.backend,
                    "exit": ev.exit_code,
                    "cost (USD)": ev.cost_usd,
                }
                for ev in trace
            ]),
            use_container_width=True,
        )
    else:
        modules.st.info(
            f"No analytics events recorded for "
            f"`{filters.repo}#{filters.issue_input}` "
            "under the current filters."
        )


def _render_drilldown(
    *,
    st: Any,
    pd: Any,
    window: DateWindow,
    repo_filter: Optional[str],
    issue_input_parsed: Optional[int],
    event_filter: Optional[Sequence[str]],
    stage_filter: Optional[Sequence[str]],
) -> None:
    """Render the drill-down through the historical dashboard helper API."""
    modules = _DashboardModules(st=st, pd=pd, charts=None, theme=None)
    filters = _DashboardFilters(
        window=window,
        repo=repo_filter,
        issue_input=issue_input_parsed,
        events=event_filter,
        stages=stage_filter,
    )
    _render_drilldown_view(modules, filters)


if __name__ == "__main__":
    main()
