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
  topbar, filter meta, KPI strip, sparkline / delta pill, and the
  issues / skill-trigger tables.
- `orchestrator.dashboard_cards` -- the inline-HTML card family: the
  computed-insight stack, the per-card header, the backend-efficiency
  cards, the cost-coverage bar, and the reliability-tile strip.
- `orchestrator.dashboard_skill_adoption` -- the `adopt_sort` /
  `adopt_dir` sort-param parser and the sortable inline-HTML table for
  the primary per-skill session-adoption matrix.
- `orchestrator.dashboard_skill_matrix` -- the sort-param parser and
  the sortable inline-HTML table for the invocation-level per-skill
  trigger matrix (rendered as a diagnostic beneath the adoption matrix).
- `orchestrator.dashboard_reads` -- the read-orchestration layer: the
  filter-to-query adapters, the cached data-extent / filter-option and
  per-filter widget readers, the two-wave reader registries, the staged
  parallel dispatch, the static-metadata load, and the single load-timing
  log. Cache keys / TTLs, read ordering, the parallel-read toggle, and the
  `AnalyticsReadError` -> one-banner-and-stop behavior live there.
- `orchestrator.dashboard_kpi_strip` -- the KPI-strip aggregations: the
  token / throughput / rework helpers that turn a `Summary` aggregate plus
  the first-wave read rows into the four KPI tiles and the resolved /
  rejected throughput totals (`_KpiInputs` / `_build_kpi_strip_data`).
- `orchestrator.dashboard_widgets` -- the widget-rendering pipeline: the
  two-wave first / second render passes, the empty / no-data states, the
  per-issue drill-down renderer, the page footer, and the small immutable
  page-state dataclasses the pipeline threads (`_DashboardModules` ...
  `_LoadedDashboard`). The exact widget order, inline HTML/text, Plotly
  inputs, and drill-down behavior live there.

`main()` is the lazy Streamlit entrypoint. This facade keeps page
startup, the sidebar / date-range controls, and the compatibility
re-exports: it groups the imported dashboard modules, resolved filters,
and staged read plan into small immutable state objects, then hands them
to `orchestrator.dashboard_widgets` for the two-wave render.
`_render_drilldown` keeps its historical signature on the export surface
and delegates to the typed internal drill-down renderer.

The historical `orchestrator.dashboard.*` entry points that
`dashboard_state` / `dashboard_kpis` / `dashboard_html` /
`dashboard_cards` / `dashboard_kpi_strip` / `dashboard_reads` own are
re-exported below under their original name; from
`dashboard_skill_adoption` and `dashboard_skill_matrix` only their two
public entry points each (`_skill_adoption_html` /
`parse_skill_adoption_sort` and `_skill_matrix_html` /
`parse_skill_matrix_sort`) are.
Together with the `dashboard_widgets` widget / page-state members the
page pipeline and the existing dashboard tests reach through
`orchestrator.dashboard.*`, each re-export is listed in
`__all__` (the inventory `tests/test_reexport_surface.py` keeps honest).
So `streamlit run orchestrator/dashboard.py`, the historical helper
surface, and the tests keep working without touching the extracted
modules. The leaf-private internal helpers are not re-exported and stay
private to their modules: the `dashboard_cards` ratio / backend-efficiency
math (`_safe_ratio` / `_backend_efficiency_metrics`), the
`dashboard_kpi_strip` KPI-total aggregations (`_kpi_totals` and its
siblings), the `dashboard_html` sparkline / table internals, the
`dashboard_widgets` token / layout math helpers, and the
`dashboard_skill_adoption` / `dashboard_skill_matrix` sort / header /
row helpers.

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
the ten remaining widget reads (including the per-session
skill-adoption matrix, the skill-trigger aggregate, and the per-skill
trigger matrix). Worker threads only
return data back to the main render thread; every `st` / placeholder
write runs on the main thread.

Streamlit (and its transitive pandas), `plotly`, the chart builders
in `orchestrator.dashboard_charts`, and the theme tokens in
`orchestrator.dashboard_theme` are imported *lazily* on the `main()`
call path so the polling tick's `orchestrator.*` import surface stays free of
the dashboard's dependency footprint. The module loads without
`streamlit` or `plotly` installed -- only `streamlit run
orchestrator/dashboard.py` (or a direct `main()` call) materializes
the imports. The extracted helper modules (`dashboard_state` /
`dashboard_kpis` / `dashboard_html` / `dashboard_cards` /
`dashboard_kpi_strip` / `dashboard_skill_adoption` /
`dashboard_skill_matrix` / `dashboard_reads` /
`dashboard_widgets`) are import-light (stdlib plus `orchestrator.analytics`)
so they preserve this invariant; it is asserted by `tests/test_dashboard.py`.

Run:
    uv sync --group dashboard
    uv run streamlit run orchestrator/dashboard.py
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from time import perf_counter
from typing import Any, Optional, Sequence

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
    SkillAdoptionRow as SkillAdoptionRow,
    SkillTriggerMatrixRow as SkillTriggerMatrixRow,
    SkillTriggerRateRow as SkillTriggerRateRow,
    Summary as Summary,
)

# Compatibility re-exports. The pure helpers moved to the focused
# `dashboard_state` / `dashboard_kpis` / `dashboard_html` /
# `dashboard_cards` / `dashboard_kpi_strip` / `dashboard_skill_adoption` /
# `dashboard_skill_matrix` / `dashboard_reads` / `dashboard_widgets`
# modules; we import each one back under its original name so `main()`
# calls them as bare names, the historical `orchestrator.dashboard.*`
# surface stays intact, and the existing tests (which reach the helpers
# via `dashboard.<name>` and inspect `main()`'s source) keep working.
# From `dashboard_skill_adoption` / `dashboard_skill_matrix` only their two
# public entry points each (`_skill_adoption_html` /
# `parse_skill_adoption_sort` and `_skill_matrix_html` /
# `parse_skill_matrix_sort`) are re-exported; their internal sort / header
# / row helpers stay private to those modules.
# The redundant `as` alias marks each as an intentional re-export so ruff
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
)
from orchestrator.dashboard_state import (  # noqa: E402
    PRESET_7D as PRESET_7D,
    PRESET_ALL as PRESET_ALL,
    PRESET_CUSTOM as PRESET_CUSTOM,
    PRESET_DAYS as PRESET_DAYS,
    PRESET_INLINE_LABELS as PRESET_INLINE_LABELS,
    PRESET_LABELS as PRESET_LABELS,
    PRESET_OPTIONS as PRESET_OPTIONS,
    TZ_OFFSET_OPTIONS as TZ_OFFSET_OPTIONS,
)
from orchestrator.dashboard_state import (  # noqa: E402
    UNCONFIGURED_DB_MESSAGE as UNCONFIGURED_DB_MESSAGE,
    _TRUTHY as _TRUTHY,
    _extent_dates as _extent_dates,
    _fan_out_reads as _fan_out_reads,
    _parse_parallel_reads_flag as _parse_parallel_reads_flag,
    cache_key as cache_key,
    dashboard_parallel_reads_enabled as dashboard_parallel_reads_enabled,
    db_unconfigured_message as db_unconfigured_message,
)
from orchestrator.dashboard_state import (  # noqa: E402
    default_date_range as default_date_range,
    format_tz_offset as format_tz_offset,
    parse_issue_number as parse_issue_number,
    preset_window as preset_window,
    previous_window as previous_window,
    resolve_stage_filter as resolve_stage_filter,
    shift_ts as shift_ts,
)
# `to_window` is re-exported by attribute off the state module rather than a
# `to_window as to_window` line in the block above: pulled in bare it reads as
# a vague import (WPS347 flags the `to_`-prefix), but it is a fixed public
# helper on the `dashboard.*` surface that the date-bar controls below also
# call, so it keeps its historical name without the flagged bare import.
from orchestrator import dashboard_state as _dashboard_state  # noqa: E402

to_window = _dashboard_state.to_window
from orchestrator.dashboard_kpis import (  # noqa: E402
    DEFAULT_EXPENSIVE_LIMIT as DEFAULT_EXPENSIVE_LIMIT,
    FAILURE_RATE_BANNER_THRESHOLD as FAILURE_RATE_BANNER_THRESHOLD,
    REWORK_BUCKETS as REWORK_BUCKETS,
    UNPRICED_COST_SOURCES as UNPRICED_COST_SOURCES,
    UNPRICED_COVERAGE_THRESHOLD as UNPRICED_COVERAGE_THRESHOLD,
    InsightBanner as InsightBanner,
    compute_insights as compute_insights,
    kpi_delta as kpi_delta,
)
from orchestrator.dashboard_kpis import (  # noqa: E402
    reliability_tile_data as reliability_tile_data,
    rework_totals as rework_totals,
    top_expensive_issues as top_expensive_issues,
)
# The insight / backend-efficiency / cost-coverage / reliability-tile
# inline-HTML card family lives in `orchestrator.dashboard_cards`. Its
# builders are re-exported below under their original names so the page
# pipeline and the historical `orchestrator.dashboard.*` surface keep
# resolving to the same objects.
from orchestrator.dashboard_cards import (  # noqa: E402
    _backend_efficiency_card_html as _backend_efficiency_card_html,
    _card_header_html as _card_header_html,
    _cost_coverage_bar_html as _cost_coverage_bar_html,
    _insights_html as _insights_html,
    _reliability_tiles_html as _reliability_tiles_html,
)
from orchestrator.dashboard_html import (  # noqa: E402
    _delta_pill as _delta_pill,
    _filter_meta_html as _filter_meta_html,
    _issues_table_html as _issues_table_html,
    _kpi_strip_html as _kpi_strip_html,
    _skill_triggers_html as _skill_triggers_html,
    _sparkline_svg as _sparkline_svg,
    _topbar_html as _topbar_html,
)

# The primary per-skill session-adoption matrix -- its `adopt_sort` /
# `adopt_dir` sort-param parser and the sortable inline-HTML table -- lives
# in `orchestrator.dashboard_skill_adoption`. Both members are re-exported
# below under their original names so the page pipeline and the historical
# `orchestrator.dashboard.*` surface keep resolving to the same objects.
from orchestrator.dashboard_skill_adoption import (  # noqa: E402
    _skill_adoption_html as _skill_adoption_html,
    parse_skill_adoption_sort as parse_skill_adoption_sort,
)

# The invocation-level per-skill trigger matrix -- its sort-param parser and
# the sortable inline-HTML table, rendered as a diagnostic beneath the
# adoption matrix -- lives in `orchestrator.dashboard_skill_matrix`. Both
# members are re-exported below under their original names so the page
# pipeline and the historical `orchestrator.dashboard.*` surface keep
# resolving to the same objects.
from orchestrator.dashboard_skill_matrix import (  # noqa: E402
    _skill_matrix_html as _skill_matrix_html,
    parse_skill_matrix_sort as parse_skill_matrix_sort,
)

# The read-orchestration layer -- filter-to-query adapters, cached reader
# wrappers, reader registries, the staged parallel dispatch + two-wave data
# load, the static-metadata load, and the load-timing log -- lives in
# `orchestrator.dashboard_reads`. Every one of its members is re-exported
# below under its original name (grouped <= 8 per statement, redundant `as`
# alias marking the intentional re-export) so the page pipeline calls them
# as bare names and the historical `orchestrator.dashboard.*` surface and
# its test patch points keep resolving to the same object.
from orchestrator.dashboard_reads import (  # noqa: E402
    DEFAULT_RECENT_AGENT_EXITS as DEFAULT_RECENT_AGENT_EXITS,
    LOADING_INDICATOR_MESSAGE as LOADING_INDICATOR_MESSAGE,
    STATIC_METADATA_TTL_SECONDS as STATIC_METADATA_TTL_SECONDS,
    _DashboardReadPlan as _DashboardReadPlan,
)
from orchestrator.dashboard_reads import (  # noqa: E402
    _build_read_keys as _build_read_keys,
    _dispatch_reads as _dispatch_reads,
    _filter_list as _filter_list,
    _first_wave_readers as _first_wave_readers,
    _log_dashboard_load as _log_dashboard_load,
    _read_filter_kwargs as _read_filter_kwargs,
    _read_filtered as _read_filtered,
    _run_read_waves as _run_read_waves,
)
from orchestrator.dashboard_reads import (  # noqa: E402
    _read_backend_daily_tokens as _read_backend_daily_tokens,
    _read_backend_efficiency as _read_backend_efficiency,
    _read_cost_coverage as _read_cost_coverage,
    _read_data_extent as _read_data_extent,
    _read_filter_options as _read_filter_options,
    _read_hourly_heatmap as _read_hourly_heatmap,
    _read_prev_kpi as _read_prev_kpi,
    _read_recent_agent_exits as _read_recent_agent_exits,
)
from orchestrator.dashboard_reads import (  # noqa: E402
    _read_repo_breakdown as _read_repo_breakdown,
    _read_review_round as _read_review_round,
    _read_skill_adoption as _read_skill_adoption,
    _read_skill_trigger_matrix as _read_skill_trigger_matrix,
    _read_skill_trigger_rates as _read_skill_trigger_rates,
    _read_stage_breakdown as _read_stage_breakdown,
    _read_static_metadata as _read_static_metadata,
    _read_summary as _read_summary,
)
from orchestrator.dashboard_reads import (  # noqa: E402
    _read_throughput as _read_throughput,
    _read_time_series as _read_time_series,
    _read_top_cost_issues as _read_top_cost_issues,
    _scoped_read as _scoped_read,
    _second_wave_readers as _second_wave_readers,
    _widget_readers as _widget_readers,
    _widget_task as _widget_task,
)

# The KPI-strip aggregations -- the token / throughput / rework helpers --
# live in `orchestrator.dashboard_kpi_strip`. The two members the facade
# and tests reach through `dashboard.<name>` (`_KpiInputs` /
# `_build_kpi_strip_data`) are re-exported below under their original names;
# the internal aggregation helpers stay private to that module.
from orchestrator.dashboard_kpi_strip import (  # noqa: E402
    _KpiInputs as _KpiInputs,
    _build_kpi_strip_data as _build_kpi_strip_data,
)

# The widget-rendering pipeline -- the two-wave render passes, the empty /
# no-data states, the per-issue drill-down renderer, the page footer, and
# the page-state dataclasses the pipeline threads -- lives in
# `orchestrator.dashboard_widgets`. The widget / page-state members the
# facade calls and the tests reach through `dashboard.<name>` are
# re-exported below (grouped <= 8 per statement, redundant `as` alias
# marking the intentional re-export) and listed in `__all__`. The purely
# internal token / layout math helpers are not re-exported; they stay
# private to the module.
from orchestrator.dashboard_widgets import (  # noqa: E402
    EMPTY_WINDOW_MESSAGE as EMPTY_WINDOW_MESSAGE,
    NO_DATA_MESSAGE as NO_DATA_MESSAGE,
    PLOTLY_CONFIG as PLOTLY_CONFIG,
    _DashboardControls as _DashboardControls,
    _DashboardFilters as _DashboardFilters,
    _DashboardModules as _DashboardModules,
    _DashboardPage as _DashboardPage,
)
from orchestrator.dashboard_widgets import (  # noqa: E402
    _backend_tokens_by_day as _backend_tokens_by_day,
    _load_dashboard_data as _load_dashboard_data,
    _render_activity_heatmap as _render_activity_heatmap,
    _render_chart_widgets as _render_chart_widgets,
    _render_dashboard_footer as _render_dashboard_footer,
    _render_dashboard_widgets as _render_dashboard_widgets,
)
from orchestrator.dashboard_widgets import (  # noqa: E402
    _render_drilldown_view as _render_drilldown_view,
    _render_empty_window as _render_empty_window,
    _render_first_wave as _render_first_wave,
    _render_hero_usage as _render_hero_usage,
    _render_issues_and_backends as _render_issues_and_backends,
    _render_no_data as _render_no_data,
    _render_recent_runs as _render_recent_runs,
    _render_remaining_widgets as _render_remaining_widgets,
)
from orchestrator.dashboard_widgets import (  # noqa: E402
    _render_repo_and_reliability as _render_repo_and_reliability,
    _render_skill_adoption as _render_skill_adoption,
    _render_skill_invocation_diagnostics as _render_skill_invocation_diagnostics,
    _render_skill_matrix_expander as _render_skill_matrix_expander,
    _render_skill_triggers as _render_skill_triggers,
    _render_stage_review_bars as _render_stage_review_bars,
    _render_topbar_and_meta as _render_topbar_and_meta,
)

# Canonical inventory of the `orchestrator.dashboard.*` surface: the page
# entrypoint (`main`) and its drill-down helper, and every name re-exported
# above from `dashboard_state` / `dashboard_kpis` / `dashboard_html` /
# `dashboard_cards` / `dashboard_kpi_strip` / `dashboard_skill_adoption` /
# `dashboard_skill_matrix` / `dashboard_reads` / `dashboard_widgets` (plus
# the `analytics` / `analytics_read` module handles). Keeping the list explicit makes the
# compatibility surface auditable in one place and governs
# `from orchestrator.dashboard import *`; every `X as X` re-export above
# must appear here (`tests/test_reexport_surface.py`).
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
    "SkillAdoptionRow",
    "SkillTriggerMatrixRow",
    "SkillTriggerRateRow",
    "Summary",
    "TZ_OFFSET_OPTIONS",
    "UNCONFIGURED_DB_MESSAGE",
    "UNPRICED_COST_SOURCES",
    "UNPRICED_COVERAGE_THRESHOLD",
    "_DashboardControls",
    "_DashboardFilters",
    "_DashboardModules",
    "_DashboardPage",
    "_DashboardReadPlan",
    "_KpiInputs",
    "_TRUTHY",
    "_backend_efficiency_card_html",
    "_backend_tokens_by_day",
    "_build_kpi_strip_data",
    "_build_read_keys",
    "_card_header_html",
    "_cost_coverage_bar_html",
    "_delta_pill",
    "_dispatch_reads",
    "_extent_dates",
    "_fan_out_reads",
    "_filter_list",
    "_filter_meta_html",
    "_first_wave_readers",
    "_insights_html",
    "_issues_table_html",
    "_kpi_strip_html",
    "_load_dashboard_data",
    "_log_dashboard_load",
    "_parse_parallel_reads_flag",
    "_read_backend_daily_tokens",
    "_read_backend_efficiency",
    "_read_cost_coverage",
    "_read_data_extent",
    "_read_filter_kwargs",
    "_read_filter_options",
    "_read_filtered",
    "_read_hourly_heatmap",
    "_read_prev_kpi",
    "_read_recent_agent_exits",
    "_read_repo_breakdown",
    "_read_review_round",
    "_read_skill_adoption",
    "_read_skill_trigger_matrix",
    "_read_skill_trigger_rates",
    "_read_stage_breakdown",
    "_read_static_metadata",
    "_read_summary",
    "_read_throughput",
    "_read_time_series",
    "_read_top_cost_issues",
    "_reliability_tiles_html",
    "_render_activity_heatmap",
    "_render_chart_widgets",
    "_render_dashboard_footer",
    "_render_dashboard_widgets",
    "_render_drilldown",
    "_render_drilldown_view",
    "_render_empty_window",
    "_render_first_wave",
    "_render_hero_usage",
    "_render_issues_and_backends",
    "_render_no_data",
    "_render_recent_runs",
    "_render_remaining_widgets",
    "_render_repo_and_reliability",
    "_render_skill_adoption",
    "_render_skill_invocation_diagnostics",
    "_render_skill_matrix_expander",
    "_render_skill_triggers",
    "_render_stage_review_bars",
    "_render_topbar_and_meta",
    "_run_read_waves",
    "_scoped_read",
    "_second_wave_readers",
    "_skill_adoption_html",
    "_skill_matrix_html",
    "_skill_triggers_html",
    "_sparkline_svg",
    "_topbar_html",
    "_widget_readers",
    "_widget_task",
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
    "parse_skill_adoption_sort",
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


def main() -> None:
    """Run the Streamlit analytics page with lazily loaded dependencies."""
    import streamlit as st

    _run_dashboard(st)


@dataclass(frozen=True)
class _SidebarSelections:
    repo: str
    events: Sequence[str]
    stages: Sequence[str]
    issue_input: str


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
