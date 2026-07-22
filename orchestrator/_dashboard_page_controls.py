# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Sidebar controls and dashboard read-plan preparation."""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Sequence

from orchestrator import _dashboard_date_range as date_range
from orchestrator import dashboard_reads, dashboard_state, dashboard_widgets


@dataclass(frozen=True)
class _SidebarSelections:
    repo: str
    events: Sequence[str]
    stages: Sequence[str]
    issue_input: str


def _timezone_choice(st: Any) -> int:
    if "tz_offset_hours" not in st.session_state:
        st.session_state.tz_offset_hours = dashboard_state.DEFAULT_TZ_OFFSET_HOURS
    return int(st.session_state.tz_offset_hours)


def _resolve_dashboard_filters(
    window: dashboard_state.DateWindow,
    selections: _SidebarSelections,
    options: Any,
) -> dashboard_widgets._DashboardFilters:
    repo = None
    if selections.repo != "All":
        repo = selections.repo
    return dashboard_widgets._DashboardFilters(
        window=window,
        repo=repo,
        issue_input=dashboard_state.parse_issue_number(selections.issue_input),
        events=list(selections.events),
        stages=dashboard_state.resolve_stage_filter(
            selections.stages,
            options.stages,
        ),
    )


def _render_dashboard_controls(
    modules: dashboard_widgets._DashboardModules,
    extent: Any,
    options: Any,
) -> dashboard_widgets._DashboardControls:
    selections = _render_sidebar_filters(st=modules.st, options=options)
    timezone_offset = _timezone_choice(modules.st)
    topbar_slot = modules.st.empty()
    window_meta = date_range._render_date_filter_bar(
        st=modules.st,
        extent=extent,
        extent_min_d=extent.min_ts.date(),
        extent_max_d=extent.max_ts.date(),
    )
    return dashboard_widgets._DashboardControls(
        filters=_resolve_dashboard_filters(window_meta[0], selections, options),
        topbar_slot=topbar_slot,
        meta_slot=window_meta[1],
        timezone_offset=timezone_offset,
    )


def _prepare_dashboard_page(
    modules: dashboard_widgets._DashboardModules,
    extent: Any,
    options: Any,
) -> dashboard_widgets._DashboardPage:
    controls = _render_dashboard_controls(modules, extent, options)
    keys = dashboard_reads._build_read_keys(
        window=controls.filters.window,
        repo_filter=controls.filters.repo,
        event_filter=controls.filters.events,
        stage_filter=controls.filters.stages,
        issue_filter=controls.filters.issue,
    )
    readers = dashboard_reads._widget_readers(
        st=modules.st,
        key=keys[0],
        prev_key=keys[1],
        tz_offset_choice=controls.timezone_offset,
    )
    return dashboard_widgets._DashboardPage(
        extent=extent,
        controls=controls,
        reads=dashboard_reads._DashboardReadPlan(
            first_wave=readers[0],
            second_wave=readers[1],
            parallel=dashboard_state.dashboard_parallel_reads_enabled(),
            started_at=perf_counter(),
        ),
    )


def _render_sidebar_filters(*, st: Any, options: Any) -> _SidebarSelections:
    """Render sidebar filters and return their unresolved selections."""
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
