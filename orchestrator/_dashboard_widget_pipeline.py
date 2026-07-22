# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Two-wave dashboard widget rendering pipeline."""
from __future__ import annotations

from datetime import timedelta
import importlib
import typing

from orchestrator.analytics import read as analytics_read
from orchestrator import _dashboard_widget_models as models
from orchestrator import dashboard_html
from orchestrator.dashboard_cards import _insights_html
from orchestrator.dashboard_kpi_strip import _KpiInputs
from orchestrator.dashboard_kpis import compute_insights


DASHBOARD_MODULE = "orchestrator.dashboard"


def _render_topbar_and_meta(
    modules: models._DashboardModules,
    page: models._DashboardPage,
    summary: analytics_read.Summary,
) -> None:
    page.controls.topbar_slot.markdown(
        dashboard_html._topbar_html(
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
        dashboard_html._filter_meta_html(
            from_d=filters.window.start.date(),
            to_d=(filters.window.end - timedelta(days=1)).date(),
            days=filters.days,
            runs=summary.total_agent_runs,
            fmt_num=modules.theme.fmt_num,
        ),
        unsafe_allow_html=True,
    )


def _render_dashboard_insights(
    modules: models._DashboardModules,
    summary: analytics_read.Summary,
    cost_coverage_rows: typing.Sequence[analytics_read.CostCoverageRow],
) -> None:
    banners = compute_insights(summary, cost_coverage_rows=cost_coverage_rows)
    if banners:
        modules.st.markdown(_insights_html(banners), unsafe_allow_html=True)


def _render_first_wave(
    modules: models._DashboardModules,
    page: models._DashboardPage,
    read_results: dict[str, typing.Any],
) -> typing.Optional[models._DashboardKpis]:
    dashboard_module = importlib.import_module(DASHBOARD_MODULE)

    summary = read_results["summary"]
    dashboard_module._render_topbar_and_meta(modules, page, summary)
    if summary.total_events == 0:
        dashboard_module._render_empty_window(modules, page)
        return None
    _render_dashboard_insights(
        modules,
        summary,
        read_results["cost_coverage_rows"],
    )
    kpi_values = dashboard_module._build_kpi_strip_data(
        _KpiInputs(
            theme=modules.theme,
            summary=summary,
            prev_summary=read_results["prev_summary"],
            ts_points=read_results["ts_points"],
            throughput_rows=read_results["throughput_rows"],
            review_round_rows=read_results["review_round_rows"],
            days_in_window=page.controls.filters.days,
        )
    )
    modules.st.markdown(
        dashboard_html._kpi_strip_html(kpi_values[0]),
        unsafe_allow_html=True,
    )
    return models._DashboardKpis(*kpi_values)


def _load_dashboard_data(
    modules: models._DashboardModules,
    page: models._DashboardPage,
) -> typing.Optional[models._LoadedDashboard]:
    dashboard_module = importlib.import_module(DASHBOARD_MODULE)

    loaded = dashboard_module._run_read_waves(
        page.reads,
        st=modules.st,
        render_first_wave=lambda read_results: dashboard_module._render_first_wave(
            modules,
            page,
            read_results,
        ),
    )
    if loaded is None:
        return None
    read_results, kpis = loaded
    return models._LoadedDashboard(read_results=read_results, kpis=kpis)


def _render_chart_widgets(
    modules: models._DashboardModules,
    page: models._DashboardPage,
    loaded: models._LoadedDashboard,
) -> None:
    dashboard_module = importlib.import_module(DASHBOARD_MODULE)

    dashboard_module._render_hero_usage(
        st=modules.st,
        dashboard_charts=modules.charts,
        ts_points=loaded.read_results["ts_points"],
        backend_daily_rows=loaded.read_results["backend_daily_rows"],
    )
    dashboard_module._render_stage_review_bars(
        st=modules.st,
        dashboard_charts=modules.charts,
        stage_rows=loaded.read_results["stage_rows"],
        review_round_rows=loaded.read_results["review_round_rows"],
    )
    dashboard_module._render_issues_and_backends(
        st=modules.st,
        theme=modules.theme,
        issues_rows=loaded.read_results["issues_rows"],
        backend_rows=loaded.read_results["backend_rows"],
        cost_coverage_rows=loaded.read_results["cost_coverage_rows"],
    )
    dashboard_module._render_repo_and_reliability(
        modules,
        models._ReliabilityPanelData(
            repos=loaded.read_results["repo_rows"],
            summary=loaded.read_results["summary"],
            throughput=loaded.read_results["throughput_rows"],
            window=page.controls.filters.window,
            resolved=loaded.kpis.resolved,
            rejected=loaded.kpis.rejected,
        ),
    )
    dashboard_module._render_activity_heatmap(
        st=modules.st,
        dashboard_charts=modules.charts,
        heatmap_rows=loaded.read_results["heatmap_rows"],
        tz_offset_choice=page.controls.timezone_offset,
    )


def _render_remaining_widgets(
    modules: models._DashboardModules,
    page: models._DashboardPage,
    loaded: models._LoadedDashboard,
) -> None:
    dashboard_module = importlib.import_module(DASHBOARD_MODULE)

    dashboard_module._render_skill_adoption(
        st=modules.st,
        skill_adoption_rows=loaded.read_results["skill_adoption_rows"],
        skill_rows=loaded.read_results["skill_rows"],
        skill_matrix_rows=loaded.read_results["skill_matrix_rows"],
    )
    dashboard_module._render_recent_runs(
        st=modules.st,
        pd=modules.pd,
        agent_exits=loaded.read_results["agent_exits"],
        tz_offset_choice=page.controls.timezone_offset,
    )
    dashboard_module._render_drilldown_view(modules, page.controls.filters)
    dashboard_module._render_dashboard_footer(
        modules,
        page.controls.filters,
        loaded.read_results["summary"],
    )


def _render_dashboard_widgets(
    modules: models._DashboardModules,
    page: models._DashboardPage,
    loaded: models._LoadedDashboard,
) -> None:
    dashboard_module = importlib.import_module(DASHBOARD_MODULE)

    dashboard_module._render_chart_widgets(modules, page, loaded)
    dashboard_module._render_remaining_widgets(modules, page, loaded)
