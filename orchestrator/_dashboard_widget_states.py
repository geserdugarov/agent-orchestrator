# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard footer and empty-state rendering."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from orchestrator.analytics.read import DataExtent, Summary
from orchestrator import _dashboard_widget_models as models
from orchestrator.dashboard_html import _topbar_html


NO_DATA_MESSAGE = (
    "No analytics events have been recorded yet. Run "
    "`uv run python -m orchestrator.analytics.sync` after some "
    "workflow activity to populate the dashboard."
)
EMPTY_WINDOW_MESSAGE = (
    "No analytics events match the current filters. Broaden the window "
    "or clear a filter to see activity."
)


def _render_dashboard_footer(
    modules: models._DashboardModules,
    filters: models._DashboardFilters,
    summary: Summary,
) -> None:
    end_date = (filters.window.end - timedelta(days=1)).date()
    window_start = filters.window.start.date().isoformat()
    agent_runs = modules.theme.fmt_num(summary.total_agent_runs)
    modules.st.markdown(
        '<div class="orch-foot">'
        f"Real data · window {window_start} → {end_date.isoformat()} · "
        f"{agent_runs} agent runs</div>",
        unsafe_allow_html=True,
    )


def _render_no_data(*, st: Any, extent: DataExtent, theme: Any) -> None:
    """Render the no-data startup state and stop."""
    st.markdown(
        _topbar_html(
            extent=extent,
            distinct_repos=0,
            total_events=0,
            spend_in_range=float(),
            fmt_money_exact=theme.fmt_money_exact,
            fmt_num=theme.fmt_num,
        ),
        unsafe_allow_html=True,
    )
    st.info(NO_DATA_MESSAGE)
    st.stop()


def _render_empty_window(
    modules: models._DashboardModules,
    page: models._DashboardPage,
) -> None:
    """Render an empty filtered window and skip the second read wave."""
    from orchestrator import dashboard as _dashboard

    _dashboard._log_dashboard_load(
        load_start=page.reads.started_at,
        reads=len(page.reads.first_wave),
        parallel=page.reads.parallel,
    )
    modules.st.info(EMPTY_WINDOW_MESSAGE)
    _dashboard._render_drilldown_view(modules, page.controls.filters)
