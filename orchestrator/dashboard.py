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
  sparkline / delta pill, the issues / skill-trigger tables, and the
  per-skill trigger matrix.

Every helper is re-exported below under its original name so
`streamlit run orchestrator/dashboard.py`, the historical
`orchestrator.dashboard.*` helper surface, and the existing dashboard
tests keep working without touching the extracted modules.

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
`orchestrator.dashboard_theme` are imported *lazily* inside `main()`
so the polling tick's `orchestrator.*` import surface stays free of
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

import html
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Optional, Sequence

# Streamlit's documented launch -- `streamlit run orchestrator/dashboard.py`
# -- executes this file as a top-level script via `runpy` with no parent
# package. The Streamlit launcher prepends the script's own directory
# (i.e. `orchestrator/`) to `sys.path`, NOT the repo root, so a
# `from . import ...` raises `ImportError: attempted relative import with
# no known parent package` before any Streamlit code can render and a
# bare `from orchestrator import ...` would fail too. Adding the repo
# root (parent of `orchestrator/`) to `sys.path` makes the absolute
# imports below work in both contexts: script-launched and package-
# imported (`import orchestrator.dashboard`). The insert is idempotent
# -- in the package case the entry is already present and the check
# is a no-op.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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
    _card_header_html as _card_header_html,
    _delta_pill as _delta_pill,
    _filter_meta_html as _filter_meta_html,
    _insights_html as _insights_html,
    _issues_table_html as _issues_table_html,
    _kpi_strip_html as _kpi_strip_html,
    _skill_matrix_html as _skill_matrix_html,
    _skill_triggers_html as _skill_triggers_html,
    _sparkline_svg as _sparkline_svg,
    _topbar_html as _topbar_html,
)

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


def main() -> None:
    """Streamlit entrypoint.

    Imports Streamlit, pandas, plotly, the chart builders, and the
    theme tokens lazily so the orchestrator polling path never pulls
    them in. Run via `streamlit run orchestrator/dashboard.py`;
    Streamlit invokes the script with `__name__ == "__main__"`, which
    falls through to the sentinel at the bottom of this file.
    """
    import pandas as pd
    import streamlit as st

    from orchestrator import dashboard_charts, dashboard_theme as theme

    st.set_page_config(
        page_title="Orchestrator Analytics",
        layout="wide",
    )
    st.markdown(theme.PAGE_CSS, unsafe_allow_html=True)

    unset = db_unconfigured_message()
    if unset:
        st.warning(unset)
        st.stop()

    # `get_data_extent` and `get_filter_options` carry no filter
    # inputs (the cache key is empty), so they tolerate a longer TTL
    # than the per-filter window reads -- the values only change as
    # `analytics.sync` ingests new events. Cache them under
    # `STATIC_METADATA_TTL_SECONDS` (5 min) so the sidebar / topbar
    # do not re-pay a round-trip on every Streamlit rerun.
    @st.cache_data(
        show_spinner=False, ttl=STATIC_METADATA_TTL_SECONDS
    )
    def _read_data_extent():
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_data_extent(conn=conn)

    @st.cache_data(
        show_spinner=False, ttl=STATIC_METADATA_TTL_SECONDS
    )
    def _read_filter_options():
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_filter_options(conn=conn)

    try:
        extent = _read_data_extent()
        options = _read_filter_options()
    except analytics_read.AnalyticsReadError as e:
        st.error(
            "Could not load analytics filter options: "
            f"{e}. Verify `ANALYTICS_DB_URL` and that the Postgres "
            "service is reachable, then reload."
        )
        st.stop()

    if extent.min_ts is None or extent.max_ts is None:
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

    extent_min_d = extent.min_ts.date()
    extent_max_d = extent.max_ts.date()

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

    # Timezone selector lives inside the "When agents run" block (see
    # the heatmap card below), but the offset is read here so the
    # second-wave fan-out can bucket the heatmap in the chosen zone.
    # Seeding session_state on first render lets the selectbox default
    # to UTC+7 while subsequent renders read whatever the operator
    # picked. The widget is wired with `key="tz_offset_hours"` further
    # down so it round-trips through this same session_state slot.
    if "tz_offset_hours" not in st.session_state:
        st.session_state.tz_offset_hours = DEFAULT_TZ_OFFSET_HOURS
    tz_offset_choice = int(st.session_state.tz_offset_hours)

    # Topbar: title + spend pill. We render a placeholder spend now
    # so it occupies the right slot; we replace it after the summary
    # query lands below.
    topbar_slot = st.empty()

    # Filter bar: presets + date inputs + range meta inside a bordered
    # container styled as the "Date range" card. Preset state persists
    # in session_state so a custom date pick survives reruns.
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
        # Single-line filter bar: label · preset switch · From · To ·
        # range meta, all bottom-aligned so the short controls (label,
        # radio, meta) sit on the same baseline as the taller date
        # inputs.
        (
            fb_label,
            fb_preset,
            fb_from,
            fb_to,
            fb_meta,
        ) = st.columns(
            [1.0, 1.7, 1.4, 1.4, 3.0], vertical_alignment="bottom"
        )
        with fb_label:
            st.markdown(
                '<div class="orch-filterbar-anchor"></div>'
                '<span class="orch-filter-label">Date range</span>',
                unsafe_allow_html=True,
            )
        with fb_preset:
            preset_choice = st.radio(
                "Range preset",
                options=(PRESET_3D, PRESET_7D, PRESET_ALL),
                format_func=lambda p: PRESET_INLINE_LABELS[p],
                index=(
                    (PRESET_3D, PRESET_7D, PRESET_ALL).index(
                        st.session_state.preset
                    )
                    if st.session_state.preset
                    in (PRESET_3D, PRESET_7D, PRESET_ALL)
                    else 2
                ),
                horizontal=True,
                label_visibility="collapsed",
                key="_preset_radio",
            )
        initial_window = (
            preset_window(preset_choice, extent)
            or to_window(extent_min_d, extent_max_d)
        )
        with fb_from:
            start_date = st.date_input(
                "From",
                value=initial_window.start.date(),
                min_value=extent_min_d,
                max_value=extent_max_d,
            )
        with fb_to:
            end_default = (initial_window.end - timedelta(days=1)).date()
            end_date = st.date_input(
                "To",
                value=end_default,
                min_value=extent_min_d,
                max_value=extent_max_d,
            )
        # Range meta ("… → … · N days · N runs") sits in the last column
        # of the same row. Rendered after the summary query lands below
        # (the run count is not known yet), so capture the slot now.
        with fb_meta:
            meta_slot = st.empty()
    window = to_window(start_date, end_date)
    st.session_state.preset = preset_choice

    repo_filter = None if repo_choice == "All" else repo_choice
    issue_input_parsed = parse_issue_number(issue_input)
    issue_filter = (
        issue_input_parsed if repo_filter is not None else None
    )
    event_filter = list(event_choice)
    stage_filter = resolve_stage_filter(stage_choice, options.stages)

    key = cache_key(
        window, repo_filter, event_filter, stage_filter, issue_filter
    )
    prev_w = previous_window(window)
    prev_key = cache_key(
        prev_w, repo_filter, event_filter, stage_filter, issue_filter
    )

    # Connection scoping: each cached wrapper checks out a thread-local
    # analytics connection inside its body via `analytics_connection()`
    # rather than threading a connection through the cache key (a raw
    # psycopg.Connection is not hashable and would crash the wrapper,
    # and every reload would otherwise look like a cache miss). The
    # thread-local persists across wrappers in the same render pass,
    # so the first cache-miss pays the ~1 s psycopg handshake and the
    # remaining 13 reads reuse the open socket. On a broken-connection
    # error inside a `with` block the CM closes the cached socket so
    # the next wrapper opens a fresh one. The cache key stays
    # `(start, end, repo, events_t, stages_t, issue)` -- exactly the
    # filter tuple, which is what we want anyway.

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_summary(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_summary(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_prev_kpi(start, end, repo, events_t, stages_t, issue):
        # Previous-window read for the KPI delta pills and
        # cost-trend banner only. The full `get_summary` shape (per-
        # event / per-stage breakdowns, distinct-issue / distinct-
        # repo counts, failure / timeout counters) is never read off
        # `prev_summary`, so a thinner reader saves a `GROUP BY`
        # follow-up plus a couple of `COUNT(DISTINCT)`s on every
        # cold load while leaving the cached wrapper shape (and
        # cache key) identical to `_read_summary`.
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_kpi_prev(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_time_series(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_time_series(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_stage_breakdown(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_stage_breakdown(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_recent_agent_exits(
        start, end, repo, events_t, stages_t, issue
    ):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_recent_agent_exits(
                limit=DEFAULT_RECENT_AGENT_EXITS,
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_top_cost_issues(start, end, repo, events_t, stages_t, issue):
        # Ask the database for the top-cost issues directly. Reading
        # the latest N issues by `last_seen` and re-sorting in Python
        # silently drops older high-cost issues that fall outside the
        # truncated set, so the redesigned "Most expensive issues"
        # panel must be cost-ordered at the SQL layer.
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_issues(
                limit=DEFAULT_EXPENSIVE_LIMIT,
                sort_by=analytics_read.SORT_BY_COST,
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_review_round(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_review_round_breakdown(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_backend_efficiency(
        start, end, repo, events_t, stages_t, issue
    ):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_backend_efficiency(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_repo_breakdown(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_repo_breakdown(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_cost_coverage(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_cost_coverage(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_hourly_heatmap(
        start, end, repo, events_t, stages_t, issue, tz_offset_hours,
    ):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_hourly_heatmap(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                tz_offset_hours=tz_offset_hours,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_throughput(start, end, repo, events_t, stages_t, issue):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_throughput_breakdown(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_backend_daily_tokens(
        start, end, repo, events_t, stages_t, issue
    ):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_backend_daily_tokens(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_skill_trigger_rates(
        start, end, repo, events_t, stages_t, issue
    ):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_skill_trigger_rates(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_skill_trigger_matrix(
        start, end, repo, events_t, stages_t, issue
    ):
        with analytics_read.analytics_connection() as conn:
            return analytics_read.get_skill_trigger_matrix(
                start=start, end=end, repo=repo,
                events=list(events_t) if events_t is not None else None,
                stages=list(stages_t) if stages_t is not None else None,
                issue=issue,
                conn=conn,
            )

    # Read fan-out. Each entry is `(name, zero-arg callable)` so
    # `_fan_out_reads` can dispatch them across worker threads when
    # `DASHBOARD_PARALLEL_READS` is set; the sequential path stays in
    # this thread under the existing thread-local `analytics_connection`.
    # Lambdas close over `key` / `prev_key` so the executor never has
    # to thread filter tuples through the futures.
    #
    # Split into two staged waves so the topbar / filter meta /
    # insight banners / KPI strip paint as soon as their inputs are
    # available instead of blocking on every widget. The first wave
    # carries the six reads those above-the-fold widgets consume
    # (`summary`, `prev_summary`, `ts_points`, `review_round_rows`,
    # `throughput_rows`, `cost_coverage_rows`); the second wave runs
    # the nine remaining widget reads. Worker threads only return
    # data back to this render thread -- every `st.*` / placeholder
    # write happens on the main thread between waves.
    first_wave_readers: list[tuple[str, Callable[[], Any]]] = [
        ("summary", lambda: _read_summary(*key)),
        ("prev_summary", lambda: _read_prev_kpi(*prev_key)),
        ("ts_points", lambda: _read_time_series(*key)),
        ("review_round_rows", lambda: _read_review_round(*key)),
        ("throughput_rows", lambda: _read_throughput(*key)),
        ("cost_coverage_rows", lambda: _read_cost_coverage(*key)),
    ]
    second_wave_readers: list[tuple[str, Callable[[], Any]]] = [
        ("stage_rows", lambda: _read_stage_breakdown(*key)),
        ("agent_exits", lambda: _read_recent_agent_exits(*key)),
        ("issues_rows", lambda: _read_top_cost_issues(*key)),
        ("backend_rows", lambda: _read_backend_efficiency(*key)),
        ("repo_rows", lambda: _read_repo_breakdown(*key)),
        ("heatmap_rows", lambda: _read_hourly_heatmap(
            *key, int(tz_offset_choice),
        )),
        ("backend_daily_rows", lambda: _read_backend_daily_tokens(*key)),
        ("skill_rows", lambda: _read_skill_trigger_rates(*key)),
        ("skill_matrix_rows", lambda: _read_skill_trigger_matrix(*key)),
    ]
    total_reads = len(first_wave_readers) + len(second_wave_readers)
    parallel = dashboard_parallel_reads_enabled()
    load_start = perf_counter()
    # Single inline spinner spans both waves -- the topbar / KPI
    # strip rendered between waves provides progressive feedback
    # while the second wave finishes, and the spinner clears once
    # every widget has its data. UI writes always run on this main
    # thread (the worker threads `_fan_out_reads` spawns only return
    # data back through the futures), so the staged renders below
    # never reach Streamlit from a worker.
    with st.spinner(LOADING_INDICATOR_MESSAGE):
        try:
            results = _fan_out_reads(
                first_wave_readers, parallel=parallel
            )
        except analytics_read.AnalyticsReadError as e:
            st.error(
                f"Analytics query failed: {e}. The dashboard cannot render "
                "without database access; check Postgres connectivity and "
                "reload."
            )
            st.stop()
        summary = results["summary"]
        prev_summary = results["prev_summary"]
        ts_points = results["ts_points"]
        review_round_rows = results["review_round_rows"]
        throughput_rows = results["throughput_rows"]
        cost_coverage_rows = results["cost_coverage_rows"]

        # Topbar / filter meta paint on the first-wave results so the
        # user sees real content before the second wave fires.
        topbar_slot.markdown(
            _topbar_html(
                extent=extent,
                distinct_repos=summary.distinct_repos,
                total_events=summary.total_events,
                spend_in_range=summary.total_cost_usd,
                fmt_money_exact=theme.fmt_money_exact,
                fmt_num=theme.fmt_num,
            ),
            unsafe_allow_html=True,
        )
        days_in_window = max((window.end - window.start).days, 1)
        meta_slot.markdown(
            _filter_meta_html(
                from_d=window.start.date(),
                to_d=(window.end - timedelta(days=1)).date(),
                days=days_in_window,
                runs=summary.total_agent_runs,
                fmt_num=theme.fmt_num,
            ),
            unsafe_allow_html=True,
        )

        if summary.total_events == 0:
            # Empty window -- skip the second wave entirely. Log the
            # short-circuit so the A/B comparison still has a line.
            log.info(
                "dashboard.load: total=%.1fs reads=%d parallel=%s",
                perf_counter() - load_start,
                len(first_wave_readers),
                "true" if parallel else "false",
            )
            st.info(EMPTY_WINDOW_MESSAGE)
            _render_drilldown(
                st=st,
                pd=pd,
                window=window,
                repo_filter=repo_filter,
                issue_input_parsed=issue_input_parsed,
                event_filter=event_filter,
                stage_filter=stage_filter,
            )
            return

        # Insights + KPI strip ---------------------------------------
        # Token totals include cache_read + cache_write so the
        # headline figure matches the standalone mock's
        # `input + output + cache_read + cache_write` accounting; the
        # `cached_tokens` cumulative column is deliberately excluded
        # so the cache band is not double-counted.
        banners = compute_insights(
            summary,
            cost_coverage_rows=cost_coverage_rows,
        )
        if banners:
            st.markdown(_insights_html(banners), unsafe_allow_html=True)
        total_cost = float(summary.total_cost_usd or 0.0)
        total_tokens = int(
            (summary.total_input_tokens or 0)
            + (summary.total_output_tokens or 0)
            + (summary.total_cache_read_tokens or 0)
            + (summary.total_cache_write_tokens or 0)
        )
        total_cost_prev = float(prev_summary.total_cost_usd or 0.0)
        total_tokens_prev = int(
            (prev_summary.total_input_tokens or 0)
            + (prev_summary.total_output_tokens or 0)
            + (prev_summary.total_cache_read_tokens or 0)
            + (prev_summary.total_cache_write_tokens or 0)
        )
        resolved = sum(int(r.resolved or 0) for r in throughput_rows)
        rejected = sum(int(r.rejected or 0) for r in throughput_rows)
        rr_total_cost, rr_rework_cost = rework_totals(review_round_rows)
        rework_share = (
            (rr_rework_cost / rr_total_cost) if rr_total_cost > 0 else 0.0
        )

        # Sparkline series, one entry per day in the window. Daily
        # tokens mirror the KPI accounting and include the cache band.
        days = sorted({p.day for p in ts_points})
        days_index = {d: i for i, d in enumerate(days)}
        daily_cost = [0.0] * len(days)
        daily_tokens = [0.0] * len(days)
        for p in ts_points:
            i = days_index[p.day]
            daily_cost[i] += float(p.cost_usd or 0.0)
            daily_tokens[i] += float(
                (p.input_tokens or 0)
                + (p.output_tokens or 0)
                + (p.cache_read_tokens or 0)
                + (p.cache_write_tokens or 0)
            )
        done_index = {r.day: int(r.resolved or 0) for r in throughput_rows}
        daily_done = [done_index.get(d, 0) for d in days]

        kpis = [
            {
                "label": "Total spend",
                "value": theme.fmt_money_exact(total_cost),
                "delta": kpi_delta(total_cost, total_cost_prev),
                "sub": (
                    f"{theme.fmt_money(total_cost / days_in_window)}/day"
                ),
                "spark": daily_cost,
                "spark_color": theme.ACCENT,
            },
            {
                "label": "Total tokens",
                "value": theme.fmt_tokens(total_tokens),
                "delta": kpi_delta(total_tokens, total_tokens_prev),
                "sub": f"{theme.fmt_tokens(total_tokens / days_in_window)}/day",
                "spark": daily_tokens,
                "spark_color": theme.TOKEN_TYPE_COLORS["Input"],
            },
            {
                "label": "Cost / resolved issue",
                "value": (
                    f"${total_cost / resolved:,.2f}"
                    if resolved > 0 else "—"
                ),
                "delta": None,
                "sub": f"{resolved} resolved · {rejected} rejected",
                "spark": daily_done,
                "spark_color": theme.TOKEN_TYPE_COLORS["Cache"],
            },
            {
                "label": "Rework share",
                "value": f"{rework_share * 100:.0f}%",
                "delta": None,
                "sub": (
                    f"{theme.fmt_money_exact(rr_rework_cost)} in review "
                    "rounds >= 1"
                ),
                "spark": None,
            },
        ]
        st.markdown(_kpi_strip_html(kpis), unsafe_allow_html=True)

        # Second wave -- the remaining widget reads. The KPI strip
        # already painted above, so the user has real content on
        # screen while the second wave finishes.
        try:
            results.update(
                _fan_out_reads(
                    second_wave_readers, parallel=parallel
                )
            )
        except analytics_read.AnalyticsReadError as e:
            st.error(
                f"Analytics query failed: {e}. The dashboard cannot render "
                "without database access; check Postgres connectivity and "
                "reload."
            )
            st.stop()
    log.info(
        "dashboard.load: total=%.1fs reads=%d parallel=%s",
        perf_counter() - load_start,
        total_reads,
        "true" if parallel else "false",
    )
    stage_rows = results["stage_rows"]
    agent_exits = results["agent_exits"]
    issues_rows = results["issues_rows"]
    backend_rows = results["backend_rows"]
    repo_rows = results["repo_rows"]
    heatmap_rows = results["heatmap_rows"]
    backend_daily_rows = results["backend_daily_rows"]
    skill_rows = results["skill_rows"]
    skill_matrix_rows = results["skill_matrix_rows"]

    # ── Hero: Spend & token usage over time ──────────────────────
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
            format_func=lambda m: (
                "By token type" if m == "type" else "By backend"
            ),
            index=0 if st.session_state.stack_mode == "type" else 1,
            horizontal=True,
            label_visibility="collapsed",
            key="_stack_mode_radio",
        )
        st.session_state.stack_mode = stack_mode

        # Build the per-day per-backend token map off
        # `get_backend_daily_tokens`, not the LIMIT-capped recent-runs
        # table -- in busy windows the cap would silently undercount
        # the "By backend" stack while the cost line and KPI tiles
        # still report the full window.
        backend_by_day: dict[date, dict[str, float]] = {}
        if stack_mode == "backend":
            for row in backend_daily_rows:
                backend_by_day.setdefault(row.day, {})
                backend_by_day[row.day][row.backend] = (
                    backend_by_day[row.day].get(row.backend, 0)
                    + int(row.total_tokens or 0)
                )

        st.plotly_chart(
            dashboard_charts.usage_over_time(
                ts_points,
                backend_rows_by_day=(
                    backend_by_day if stack_mode == "backend" else None
                ),
                mode=stack_mode,
                # The card header already renders the title; suppress
                # the in-chart title so it is not duplicated.
                title=None,
            ),
            use_container_width=True,
            config=PLOTLY_CONFIG,
        )

    # ── Stage cost (7/12) + review-cycle cost (5/12) ─────────────
    # Pin both bar panels to the same height (driven by whichever has
    # more bars) so the two cards line up bottom-to-bottom.
    bars_h = 40 * max(len(stage_rows), len(review_round_rows), 1) + 80
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

    # ── Top expensive issues (7/12) + backend efficiency (5/12) ──
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
            # `issues_rows` is already cost-ordered from the SQL
            # (`sort_by="cost"`); pipe through `top_expensive_issues`
            # so the in-memory cost / event-count tie-breakers stay
            # the source of truth and the rendered set never exceeds
            # `DEFAULT_EXPENSIVE_LIMIT`.
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
                for row in backend_rows:
                    # Per-backend token total mirrors the headline KPI:
                    # input + output + cache_read + cache_write so the
                    # "cost / 1M tok" tile divides by the same volume
                    # the rest of the redesigned page reports.
                    tokens = int(
                        (row.total_input_tokens or 0)
                        + (row.total_output_tokens or 0)
                        + (row.total_cache_read_tokens or 0)
                        + (row.total_cache_write_tokens or 0)
                    )
                    cost_per_m = (
                        (row.total_cost_usd / (tokens / 1_000_000))
                        if tokens > 0 else 0.0
                    )
                    cost_per_run = (
                        (row.total_cost_usd / row.runs)
                        if row.runs > 0 else 0.0
                    )
                    # Cache leverage: share of "billable input" served
                    # from cache. Matches the standalone mock's
                    # `cacheRead / (cacheRead + input)` accounting --
                    # high cache hit means a smaller fraction of input
                    # tokens hit the model's input rate, which is the
                    # cost lever the operator is reading off the card.
                    cache_read = int(row.total_cache_read_tokens or 0)
                    input_tok = int(row.total_input_tokens or 0)
                    cache_hit_pct = (
                        (cache_read / (cache_read + input_tok) * 100)
                        if (cache_read + input_tok) > 0 else 0.0
                    )
                    color = theme.color_for(
                        row.backend, explicit=theme.BACKEND_COLORS
                    )
                    st.markdown(
                        f'<div style="border:1px solid {theme.BORDER};'
                        f'border-radius:8px;padding:10px 12px;'
                        f'margin-bottom:8px">'
                        f'<div style="display:flex;align-items:center;'
                        f'gap:8px;margin-bottom:4px">'
                        f'<span style="display:inline-block;width:10px;'
                        f'height:10px;border-radius:50%;background:{color}">'
                        f'</span>'
                        f'<b style="color:{theme.TEXT}">'
                        f'{html.escape(row.backend)}</b>'
                        f'<span style="color:{theme.MUTED_TEXT};'
                        f'font-size:12px;margin-left:auto">'
                        f'{row.runs} runs · {theme.fmt_tokens(tokens)} tok'
                        '</span>'
                        '</div>'
                        f'<div style="color:{theme.TEXT};font-size:20px;'
                        f'font-weight:600;'
                        f'font-family:{theme.MONO_FONT_FAMILY};'
                        f'margin-bottom:6px">'
                        f'{html.escape(theme.fmt_money_exact(row.total_cost_usd))}'
                        f'<span style="color:{theme.MUTED_TEXT};'
                        f'font-size:11px;margin-left:8px;'
                        f'font-family:{theme.FONT_FAMILY}">'
                        f'spend</span></div>'
                        f'<div style="display:flex;gap:14px;font-size:12px;'
                        f'color:{theme.MUTED_TEXT}">'
                        f'<span>${cost_per_m:.2f} / 1M tok</span>'
                        f'<span>{cache_hit_pct:.0f}% cache hit</span>'
                        f'<span>${cost_per_run:.2f} / run</span>'
                        '</div></div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.info("No `agent_exit` rows match the current filters.")
            if cost_coverage_rows:
                # Token share, not run share -- a few high-token runs
                # can dominate cost while looking like a thin slice of
                # the run count, so the standalone mock sizes the bar
                # by `tok` and we follow suit. Falls back to the run
                # share when the window carries no token volume yet
                # (a brand-new database with `agent_exit` rows that
                # never reported usage).
                total_tokens = sum(
                    int(r.total_tokens or 0) for r in cost_coverage_rows
                )
                if total_tokens > 0:
                    weights = [
                        int(r.total_tokens or 0) for r in cost_coverage_rows
                    ]
                    total = total_tokens
                else:
                    weights = [int(r.runs or 0) for r in cost_coverage_rows]
                    total = sum(weights) or 1
                segs = []
                legend = []
                for r, w in zip(cost_coverage_rows, weights):
                    pct = w / total * 100
                    color = theme.color_for(
                        r.cost_source,
                        [r.cost_source for r in cost_coverage_rows],
                        explicit=theme.COST_SOURCE_COLORS,
                    )
                    segs.append(
                        f'<span style="width:{pct:.1f}%;background:{color}" '
                        f'title="{html.escape(r.cost_source)}"></span>'
                    )
                    legend.append(
                        f'<span><span class="dot" '
                        f'style="background:{color}"></span>'
                        f'{html.escape(r.cost_source)} '
                        f'<b style="color:{theme.TEXT};'
                        f'font-family:{theme.MONO_FONT_FAMILY}">'
                        f'{pct:.1f}%</b>'
                        '</span>'
                    )
                st.markdown(
                    '<div class="orch-cov-title">'
                    'Cost attribution coverage</div>'
                    f'<div class="orch-cov-bar">{"".join(segs)}</div>'
                    f'<div class="orch-cov-legend">{"".join(legend)}</div>',
                    unsafe_allow_html=True,
                )

    # ── Repo cost (7/12) + reliability tiles (5/12) ─────────────
    col_repo, col_rel = st.columns([7, 5])
    with col_repo:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Cost by repository",
                    "Spend across managed repos",
                ),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.cost_by_repo(repo_rows),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )
    with col_rel:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Reliability & throughput",
                    "Run health and issues resolved per day",
                ),
                unsafe_allow_html=True,
            )
            # Pull every reliability tile off the same full-window
            # `Summary` aggregate so e.g. the timeout count sees every
            # timed-out run in the window, not just the latest 100
            # rows surfaced by `get_recent_agent_exits`.
            raw_tiles = reliability_tile_data(
                summary, resolved=resolved, rejected=rejected,
            )
            tiles_html = "".join(
                f'<div class="orch-rel-tile {tone}">'
                f'<div class="orch-rel-value">'
                f'{html.escape(v if isinstance(v, str) else theme.fmt_num(v))}'
                f'</div>'
                f'<div class="orch-rel-label">{html.escape(lbl)}</div>'
                '</div>'
                for v, lbl, tone in raw_tiles
            )
            st.markdown(
                f'<div class="orch-rel-tiles">{tiles_html}</div>',
                unsafe_allow_html=True,
            )
            # Pass the window so every day -- including zero-
            # resolution ones the SQL elides -- renders an
            # explicit bar. Without the window the chart would
            # appear "gappy" against a calendar baseline.
            st.plotly_chart(
                dashboard_charts.done_per_day_bars(
                    throughput_rows,
                    window_start=window.start.date(),
                    window_end=(window.end - timedelta(days=1)).date(),
                    title=None,
                ),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )

    # ── When agents run (heatmap) ────────────────────────────────
    tz_label = format_tz_offset(int(tz_offset_choice))
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "When agents run",
                f"Token volume by hour ({tz_label}) × weekday",
            ),
            unsafe_allow_html=True,
        )
        # Per-card UTC-offset selector. `key="tz_offset_hours"` ties
        # it to the session_state slot seeded above so the heatmap
        # read (which already fired in the second-wave fan-out) and
        # this widget agree on the value. On change Streamlit reruns
        # the script and the next read uses the new offset.
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

    # ── Skill trigger rates ──────────────────────────────────────
    # Opt-in read-side widget over the `skills_triggered` /
    # `skills_triggered_count` fields `record_agent_exit` folds into
    # `extras` when `TRACK_SKILL_TRIGGERS` is on. A `0%` rate is a real
    # signal ("this role's skill is not firing"), but it cannot tell a
    # tracked-but-quiet run from one whose tracking was off, so the
    # caption names the switch when nothing has triggered yet.
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "Skill trigger rates",
                "Share of agent runs that triggered a skill, by role and "
                "backend (requires TRACK_SKILL_TRIGGERS)",
            ),
            unsafe_allow_html=True,
        )
        if skill_rows:
            st.markdown(
                _skill_triggers_html(skill_rows),
                unsafe_allow_html=True,
            )
            if not any(r.skill_runs for r in skill_rows):
                st.caption(
                    "No skill triggers recorded in this window. Enable "
                    "`TRACK_SKILL_TRIGGERS` (default off) so "
                    "`record_agent_exit` records which skills each run "
                    "pulls."
                )
            # Second table: the per-skill x (repo, role, backend) trigger
            # matrix. Folds each repo's skill catalog into the observed
            # triggers so an offered-but-never-triggered skill surfaces
            # as an explicit `0` cell; `_skill_matrix_html` renders a
            # clear fallback notice in place of the table when the read
            # model returns no catalog-backed matrix (no catalog records
            # matched and no run fired a skill).
            st.markdown(
                '<p class="orch-card-sub" style="margin-top:14px">'
                'Per-skill trigger matrix · which skills each '
                'repo × role × backend cohort reaches for'
                '</p>',
                unsafe_allow_html=True,
            )
            st.markdown(
                _skill_matrix_html(skill_matrix_rows),
                unsafe_allow_html=True,
            )
        else:
            st.info("No `agent_exit` rows match the current filters.")

    # ── Recent agent runs expander ───────────────────────────────
    with st.expander("Recent agent runs", expanded=False):
        if agent_exits:
            ts_offset = timedelta(hours=int(tz_offset_choice))
            df_exits = pd.DataFrame([
                {
                    "ts": shift_ts(r.ts, ts_offset),
                    "repo": r.repo,
                    "issue": r.issue,
                    "stage": r.stage,
                    "agent": r.agent_role,
                    "backend": r.backend,
                    "duration (s)": r.duration_s,
                    "exit": r.exit_code,
                    "timed out": r.timed_out,
                    "round": r.review_round,
                    "retry": r.retry_count,
                    "input tokens": r.input_tokens,
                    "output tokens": r.output_tokens,
                    "cost (USD)": r.cost_usd,
                    "cost source": r.cost_source,
                }
                for r in agent_exits
            ])
            st.dataframe(df_exits, use_container_width=True)
        else:
            st.info("No `agent_exit` rows match the current filters.")

    _render_drilldown(
        st=st,
        pd=pd,
        window=window,
        repo_filter=repo_filter,
        issue_input_parsed=issue_input_parsed,
        event_filter=event_filter,
        stage_filter=stage_filter,
    )

    st.markdown(
        '<div class="orch-foot">'
        f'Real data · window {window.start.date().isoformat()} → '
        f'{(window.end - timedelta(days=1)).date().isoformat()} · '
        f'{theme.fmt_num(summary.total_agent_runs)} agent runs'
        '</div>',
        unsafe_allow_html=True,
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
    """Per-issue event trace section.

    Renders only when the operator typed a parseable issue number;
    when a repo is not also selected, surfaces an instructive notice
    so the empty result is not confused for a bug. Failures from the
    read model are caught and surfaced inline -- a drill-down error
    must not poison the overview the operator already scrolled past.
    """
    if issue_input_parsed is None:
        return
    st.subheader(f"Issue #{issue_input_parsed} drill-down")
    if repo_filter is None:
        st.info(
            "Pick a specific repo in the sidebar before drilling "
            "into an issue number -- GitHub issue numbers repeat "
            "across repos."
        )
        return
    try:
        with analytics_read.analytics_connection() as conn:
            trace = analytics_read.get_issue_events(
                repo=repo_filter,
                issue=issue_input_parsed,
                start=window.start,
                end=window.end,
                events=list(event_filter) if event_filter is not None else None,
                stages=list(stage_filter) if stage_filter is not None else None,
                conn=conn,
            )
    except analytics_read.AnalyticsReadError as e:
        st.error(f"Issue drill-down failed: {e}")
        return
    if trace:
        st.dataframe(
            pd.DataFrame([
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
        st.info(
            f"No analytics events recorded for "
            f"`{repo_filter}#{issue_input_parsed}` "
            "under the current filters."
        )


if __name__ == "__main__":
    main()
