# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Widget rendering for the analytics dashboard.

The Streamlit page in `orchestrator.dashboard` delegates everything
between "read-model rows in hand" and "cards on the page" to this
module: the cohesive widget-rendering pipeline.

- The token / throughput / rework KPI-strip aggregations
  (`_summary_total_tokens` ... `_build_kpi_strip_data`) that turn a
  `Summary` aggregate plus the first-wave read rows into the four KPI
  tiles and the resolved / rejected throughput totals live in
  `orchestrator.dashboard_kpi_strip`; this module builds the strip by
  handing a `_KpiInputs` to `_build_kpi_strip_data` through the facade.
- The two-wave render pipeline -- the first-wave topbar / filter-meta /
  insight / KPI paint (`_render_first_wave` and its `_render_*`
  helpers), the second-wave chart / table cards (`_render_chart_widgets`
  / `_render_remaining_widgets`), the empty / no-data states, the
  per-issue drill-down (`_render_drilldown_view`), and the page footer.
- The per-widget shaping helpers that feed Plotly / the inline-HTML
  builders (`_backend_tokens_by_day`, the stack-mode toggle vocabulary,
  the paired-bar height).

`orchestrator.dashboard` keeps page startup, the sidebar / date-range
controls, and the compatibility re-exports; it hands each widget helper
the loaded module handles, resolved filters, and read results through
the small immutable page-state dataclasses defined here
(`_DashboardModules` ... `_LoadedDashboard`). It re-exports the widget /
KPI / page-state members the pipeline and the dashboard tests reach under
their original names so the historical `orchestrator.dashboard.*` surface
keeps resolving to the same objects; the purely internal token / layout
math helpers stay private to this module.

The page-pipeline functions call their siblings, the read-wave dispatch
(`_run_read_waves` / `_log_dashboard_load`), and `PLOTLY_CONFIG` back
through the `orchestrator.dashboard` facade at call time (`from orchestrator
import dashboard as _dashboard`), not as module-local names, so
`patch.object(dashboard, ...)` on any of those re-exports intercepts the
running pipeline (mirroring the `workflow.py` stage-handler facade). The
module-private helpers, the read primitives (`_scoped_read` / `_filter_list`),
and the pure `dashboard_html` / `dashboard_cards` /
`dashboard_skill_adoption` / `dashboard_skill_matrix` /
`dashboard_kpis` / `dashboard_state` builders are called directly.

Streamlit / Plotly / pandas are never imported here: every helper that
needs `st`, a chart builder, the theme tokens, or a DataFrame takes the
loaded handle as a plain parameter (bundled in `_DashboardModules`, or
passed directly). Together with the stdlib-plus-`orchestrator` imports
below (`analytics.read`, the import-light `dashboard_state` /
`dashboard_kpis` / `dashboard_html` / `dashboard_cards` /
`dashboard_kpi_strip` / `dashboard_skill_adoption` /
`dashboard_skill_matrix` / `dashboard_reads`
helpers), this keeps the module off the polling tick's dependency
footprint; `tests/test_dashboard.py` asserts the invariant.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

from orchestrator.analytics import read as analytics_read
from orchestrator.analytics.read import (
    CostCoverageRow,
    DataExtent,
    SkillAdoptionRow,
    SkillTriggerMatrixRow,
    SkillTriggerRateRow,
    Summary,
)
from orchestrator.dashboard_cards import (
    _backend_efficiency_card_html,
    _card_header_html,
    _cost_coverage_bar_html,
    _insights_html,
    _reliability_tiles_html,
)
from orchestrator.dashboard_html import (
    _filter_meta_html,
    _issues_table_html,
    _kpi_strip_html,
    _skill_triggers_html,
    _topbar_html,
)
from orchestrator.dashboard_kpi_strip import _KpiInputs
from orchestrator.dashboard_skill_adoption import (
    _skill_adoption_html,
    parse_skill_adoption_sort,
)
from orchestrator.dashboard_skill_matrix import (
    _skill_matrix_html,
    parse_skill_matrix_sort,
)
from orchestrator.dashboard_kpis import (
    compute_insights,
    reliability_tile_data,
    top_expensive_issues,
)
from orchestrator.dashboard_reads import (
    _DashboardReadPlan,
    _filter_list,
    _scoped_read,
)
from orchestrator.dashboard_state import (
    TZ_OFFSET_OPTIONS,
    DateWindow,
    format_tz_offset,
    shift_ts,
)

# Plotly config passed to every `st.plotly_chart` call. Disabling
# the modebar keeps the hover camera/zoom/pan toolbar off the cards
# -- the standalone mock has no chart chrome, and the toolbar pops
# on hover for every chart on the page otherwise. Held as an immutable
# mapping so it cannot be mutated in place; each `plotly_chart` call is
# handed a fresh `dict(...)` copy (the exact `{"displayModeBar": False}`
# Plotly expects -- a bare mapping proxy is not JSON-serializable).
PLOTLY_CONFIG: Mapping[str, Any] = MappingProxyType({"displayModeBar": False})

NO_DATA_MESSAGE = (
    "No analytics events have been recorded yet. Run "
    "`uv run python -m orchestrator.analytics.sync` after some "
    "workflow activity to populate the dashboard."
)
EMPTY_WINDOW_MESSAGE = (
    "No analytics events match the current filters. Broaden the window "
    "or clear a filter to see activity."
)
NO_AGENT_EXITS_MESSAGE = "No `agent_exit` rows match the current filters."


# Data-table sizing (px): per-row height plus fixed header/padding base.
_TABLE_ROW_HEIGHT = 40
_TABLE_BASE_HEIGHT = 80


@dataclass(frozen=True)
class _DashboardModules:
    st: Any
    pd: Any
    charts: Any
    theme: Any


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
    filters = page.controls.filters
    page.controls.meta_slot.markdown(
        _filter_meta_html(
            from_d=filters.window.start.date(),
            to_d=(filters.window.end - timedelta(days=1)).date(),
            days=filters.days,
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
    from orchestrator import dashboard as _dashboard

    summary = read_results["summary"]
    _dashboard._render_topbar_and_meta(modules, page, summary)
    if summary.total_events == 0:
        _dashboard._render_empty_window(modules, page)
        return None
    _render_dashboard_insights(
        modules,
        summary,
        read_results["cost_coverage_rows"],
    )
    kpi_values = _dashboard._build_kpi_strip_data(_KpiInputs(
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
    from orchestrator import dashboard as _dashboard

    loaded = _dashboard._run_read_waves(
        page.reads,
        st=modules.st,
        render_first_wave=lambda read_results: _dashboard._render_first_wave(
            modules, page, read_results,
        ),
    )
    if loaded is None:
        return None
    read_results, kpis = loaded
    return _LoadedDashboard(read_results=read_results, kpis=kpis)


def _render_chart_widgets(
    modules: _DashboardModules,
    page: _DashboardPage,
    loaded: _LoadedDashboard,
) -> None:
    from orchestrator import dashboard as _dashboard

    _dashboard._render_hero_usage(
        st=modules.st,
        dashboard_charts=modules.charts,
        ts_points=loaded.read_results["ts_points"],
        backend_daily_rows=loaded.read_results["backend_daily_rows"],
    )
    _dashboard._render_stage_review_bars(
        st=modules.st,
        dashboard_charts=modules.charts,
        stage_rows=loaded.read_results["stage_rows"],
        review_round_rows=loaded.read_results["review_round_rows"],
    )
    _dashboard._render_issues_and_backends(
        st=modules.st,
        theme=modules.theme,
        issues_rows=loaded.read_results["issues_rows"],
        backend_rows=loaded.read_results["backend_rows"],
        cost_coverage_rows=loaded.read_results["cost_coverage_rows"],
    )
    _dashboard._render_repo_and_reliability(
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
    _dashboard._render_activity_heatmap(
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
    from orchestrator import dashboard as _dashboard

    _dashboard._render_skill_adoption(
        st=modules.st,
        skill_adoption_rows=loaded.read_results["skill_adoption_rows"],
        skill_rows=loaded.read_results["skill_rows"],
        skill_matrix_rows=loaded.read_results["skill_matrix_rows"],
    )
    _dashboard._render_recent_runs(
        st=modules.st,
        pd=modules.pd,
        agent_exits=loaded.read_results["agent_exits"],
        tz_offset_choice=page.controls.timezone_offset,
    )
    _dashboard._render_drilldown_view(modules, page.controls.filters)
    _dashboard._render_dashboard_footer(
        modules,
        page.controls.filters,
        loaded.read_results["summary"],
    )


def _render_dashboard_widgets(
    modules: _DashboardModules,
    page: _DashboardPage,
    loaded: _LoadedDashboard,
) -> None:
    from orchestrator import dashboard as _dashboard

    _dashboard._render_chart_widgets(modules, page, loaded)
    _dashboard._render_remaining_widgets(modules, page, loaded)


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
    from orchestrator import dashboard as _dashboard

    _dashboard._log_dashboard_load(
        load_start=page.reads.started_at,
        reads=len(page.reads.first_wave),
        parallel=page.reads.parallel,
    )
    modules.st.info(EMPTY_WINDOW_MESSAGE)
    _dashboard._render_drilldown_view(modules, page.controls.filters)


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
    from orchestrator import dashboard as _dashboard

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
            config=dict(_dashboard.PLOTLY_CONFIG),
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
    from orchestrator import dashboard as _dashboard

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
                config=dict(_dashboard.PLOTLY_CONFIG),
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
                config=dict(_dashboard.PLOTLY_CONFIG),
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
                st.info(NO_AGENT_EXITS_MESSAGE)
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
    from orchestrator import dashboard as _dashboard

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
                config=dict(_dashboard.PLOTLY_CONFIG),
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
                config=dict(_dashboard.PLOTLY_CONFIG),
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
    from orchestrator import dashboard as _dashboard

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
            config=dict(_dashboard.PLOTLY_CONFIG),
        )


def _render_skill_adoption(
    *,
    st: Any,
    skill_adoption_rows: Sequence[SkillAdoptionRow],
    skill_rows: Sequence[SkillTriggerRateRow],
    skill_matrix_rows: Sequence[SkillTriggerMatrixRow],
) -> None:
    """Render the per-session skill-adoption matrix and invocation diagnostics.

    The headline metric is *session* adoption: for each `(repo, role,
    backend, skill)` cell, what share of the logical agent sessions that
    had the skill available actually loaded it -- counted once per session,
    so a resume chain that pulled a skill across three ticks weighs one, not
    three. The window-scoped invocation loads and incidental references ride
    alongside as diagnostics; an incidental reference never counts toward
    adoption, so it can never raise the rate. The per-run trigger views stay
    reachable as a clearly named invocation-level diagnostic below.

    This is an opt-in read-side widget over the `skills_available` /
    `skills_triggered` / `skills_incidental` fields `record_agent_exit`
    folds into `extras` when `TRACK_SKILL_TRIGGERS` is on. When no rows
    surface at all the table names the switch, but any row proves tracking
    recorded evidence -- a `sessions > 0` count means availability was
    tracked, an incidental reference means the stream was parsed -- so a
    zero-adoption window with rows present captions a neutral genuine-0%
    result rather than nagging to enable an already-on switch.
    """
    from orchestrator import dashboard as _dashboard

    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "Skill adoption",
                "Share of agent sessions that loaded each available skill, "
                "by repo, role, and backend (requires TRACK_SKILL_TRIGGERS)",
            ),
            unsafe_allow_html=True,
        )
        if not skill_rows:
            st.info("No `agent_exit` rows match the current filters.")
            return

        # Clickable column headers re-sort the matrix: each header anchor
        # writes `adopt_sort` / `adopt_dir` query params, which this parses
        # back into a (column, direction) pair the render applies on top of
        # the read model's default order.
        adopt_sort_key, adopt_sort_desc = parse_skill_adoption_sort(
            st.query_params
        )
        st.markdown(
            _skill_adoption_html(
                skill_adoption_rows,
                sort_key=adopt_sort_key,
                descending=adopt_sort_desc,
            ),
            unsafe_allow_html=True,
        )
        caption = _skill_adoption_zero_caption(skill_adoption_rows)
        if caption is not None:
            st.caption(caption)
        # Any adoption row proves `TRACK_SKILL_TRIGGERS` recorded skill
        # evidence, so the diagnostics below must not tell the operator to
        # enable a switch the adoption caption just confirmed is on.
        _dashboard._render_skill_invocation_diagnostics(
            st=st,
            skill_rows=skill_rows,
            skill_matrix_rows=skill_matrix_rows,
            tracking_confirmed=bool(skill_adoption_rows),
        )


def _skill_adoption_zero_caption(
    skill_adoption_rows: Sequence[SkillAdoptionRow],
) -> Optional[str]:
    """Caption for a zero-adoption window, or None when a caption is unwanted.

    Only fires when rows are present but none were adopted. A present row is
    itself proof `TRACK_SKILL_TRIGGERS` is on -- `sessions > 0` means the
    session's `skills_available` set was recorded, a load or incidental
    reference means the stream was parsed -- so this never recommends
    enabling the switch (the empty-rows path leaves that to the table's own
    `SKILL_ADOPTION_EMPTY_MESSAGE`).

    A cohort with availability but no loads is a genuine 0% adoption. When
    every row has `sessions == 0` the rows exist only as window-scoped
    diagnostics, and the caption names the evidence the columns actually
    show -- loads (a session loaded a skill it did not report available),
    incidental references, or both -- so it never claims "incidental only"
    over a non-zero Invocation loads column. Returns None when a skill was
    adopted (no caption) or when there are no rows at all.
    """
    if not skill_adoption_rows:
        return None
    if any(row.adopted for row in skill_adoption_rows):
        return None
    if any(row.sessions for row in skill_adoption_rows):
        return (
            "Skills were available to sessions this window but none loaded "
            "one -- a genuine 0% adoption, not missing tracking."
        )
    return _skill_adoption_evidence_caption(skill_adoption_rows)


def _skill_adoption_evidence_caption(
    skill_adoption_rows: Sequence[SkillAdoptionRow],
) -> str:
    loaded = any(row.load_rows for row in skill_adoption_rows)
    incidental = any(row.incidental for row in skill_adoption_rows)
    if loaded and incidental:
        return (
            "Skills were loaded and referenced incidentally this window, but "
            "no session reported one available to adopt."
        )
    if loaded:
        return (
            "Skills were loaded this window, but no session reported one "
            "available to adopt."
        )
    return (
        "Only incidental skill references were recorded this window; no "
        "session reported a skill available to adopt."
    )


def _render_skill_invocation_diagnostics(
    *,
    st: Any,
    skill_rows: Sequence[SkillTriggerRateRow],
    skill_matrix_rows: Sequence[SkillTriggerMatrixRow],
    tracking_confirmed: bool = False,
) -> None:
    """Render the invocation-level skill diagnostics in a collapsed expander.

    The per-run trigger views -- the aggregate trigger-rate table (by role
    and backend) and the per-skill trigger matrix -- are a clearly named
    diagnostic beneath the session-adoption view rather than the headline
    metric. A run-level rate answers a different question from session
    adoption (how often a single agent invocation reached for a skill, not
    whether the logical session ever loaded it), so it folds into a
    collapsed expander so it does not dominate the card by default.

    When no run triggered a skill the zero-trigger caption depends on
    `tracking_confirmed`: with adoption evidence in hand (the caller has
    proof `TRACK_SKILL_TRIGGERS` is on) it reports a genuine no-trigger
    window, and only without that evidence does it name the switch -- so
    the diagnostic never contradicts an adoption panel that already
    confirmed tracking is on.
    """
    with st.expander(
        "Invocation-level diagnostics · per-run skill triggers",
        expanded=False,
    ):
        st.markdown(
            _skill_triggers_html(skill_rows),
            unsafe_allow_html=True,
        )
        if not any(row.skill_runs for row in skill_rows):
            if tracking_confirmed:
                st.caption(
                    "No agent run triggered a skill in this window."
                )
            else:
                st.caption(
                    "No skill triggers recorded in this window. Enable "
                    "`TRACK_SKILL_TRIGGERS` (default off) so "
                    "`record_agent_exit` records which skills each run pulls."
                )
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


def _render_skill_triggers(
    *,
    st: Any,
    skill_rows: Sequence[SkillTriggerRateRow],
    skill_matrix_rows: Sequence[SkillTriggerMatrixRow],
) -> None:
    """Compatibility entry point rendering the trigger-rates skill panel.

    A stable name on the `orchestrator.dashboard` facade for external callers
    and `patch.object(dashboard, ...)` points: renders a "Skill trigger
    rates" card with the per-`(role, backend)` aggregate table above the
    fold-out per-skill trigger matrix. The page pipeline renders the skill
    panel through `_render_skill_adoption`.
    """
    from orchestrator import dashboard as _dashboard

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
            st.info(NO_AGENT_EXITS_MESSAGE)
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
        _dashboard._render_skill_matrix_expander(
            st=st,
            skill_matrix_rows=skill_matrix_rows,
        )


def _render_skill_matrix_expander(
    *,
    st: Any,
    skill_matrix_rows: Sequence[SkillTriggerMatrixRow],
) -> None:
    """Compatibility entry point: the per-skill trigger matrix in a collapsed expander.

    A stable name on the `orchestrator.dashboard` facade for external callers
    and `patch.object(dashboard, ...)` points: renders the per-skill trigger
    matrix inside a collapsed `st.expander`. The page pipeline renders the
    matrix through `_render_skill_invocation_diagnostics`.
    """
    with st.expander(
        "Per-skill trigger matrix · which skills each "
        "repo × role × backend cohort reaches for",
        expanded=False,
    ):
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
            st.info(NO_AGENT_EXITS_MESSAGE)


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
