# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard cost, reliability, and activity cards."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Sequence

from orchestrator import _dashboard_widget_models as models
from orchestrator.dashboard_cards import (
    _backend_efficiency_card_html,
    _card_header_html,
    _cost_coverage_bar_html,
    _reliability_tiles_html,
)
from orchestrator.dashboard_html import _issues_table_html
from orchestrator.dashboard_kpis import reliability_tile_data, top_expensive_issues
from orchestrator.dashboard_state import (
    TZ_OFFSET_OPTIONS,
    format_tz_offset,
)


_TABLE_ROW_HEIGHT = 40
_TABLE_BASE_HEIGHT = 80
NO_AGENT_EXITS_MESSAGE = "No `agent_exit` rows match the current filters."


def _render_stage_review_bars(
    *,
    st: Any,
    dashboard_charts: Any,
    stage_rows: Any,
    review_round_rows: Any,
) -> None:
    """Render aligned per-stage and per-review-round cost bars."""
    from orchestrator import dashboard as _dashboard

    bars_height = _paired_bars_height(stage_rows, review_round_rows)
    stage_column, round_column = st.columns([7, 5])
    with stage_column:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Cost by workflow stage",
                    "Where spend lands across the issue lifecycle",
                ),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.cost_by_stage(stage_rows, height=bars_height),
                use_container_width=True,
                config=dict(_dashboard.PLOTLY_CONFIG),
            )
    with round_column:
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
                    review_round_rows,
                    height=bars_height,
                ),
                use_container_width=True,
                config=dict(_dashboard.PLOTLY_CONFIG),
            )


def _paired_bars_height(
    stage_rows: Sequence[Any],
    review_rows: Sequence[Any],
) -> int:
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
    """Render the top-cost issues and backend-efficiency columns."""
    issues_column, backend_column = st.columns([7, 5])
    with issues_column:
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
                st.markdown(_issues_table_html(expensive), unsafe_allow_html=True)
            else:
                st.info("No agent runs with recorded cost in this window.")
    with backend_column:
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
    modules: models._DashboardModules,
    panel: models._ReliabilityPanelData,
) -> None:
    """Render repository spend and reliability throughput."""
    from orchestrator import dashboard as _dashboard

    repo_column, reliability_column = modules.st.columns([7, 5])
    with repo_column:
        with modules.st.container(border=True):
            modules.st.markdown(
                _card_header_html("Cost by repository", "Spend across managed repos"),
                unsafe_allow_html=True,
            )
            modules.st.plotly_chart(
                modules.charts.cost_by_repo(panel.repos),
                use_container_width=True,
                config=dict(_dashboard.PLOTLY_CONFIG),
            )
    with reliability_column:
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
                    window_end=(panel.window.end - timedelta(days=1)).date(),
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
    """Render the weekday-by-hour token-volume heatmap."""
    from orchestrator import dashboard as _dashboard

    timezone_label = format_tz_offset(int(tz_offset_choice))
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "When agents run",
                f"Token volume by hour ({timezone_label}) × weekday",
            ),
            unsafe_allow_html=True,
        )
        st.selectbox(
            "Timezone",
            TZ_OFFSET_OPTIONS,
            key="tz_offset_hours",
            format_func=format_tz_offset,
            help=(
                'Shifts heatmap bucketing and the "Recent agent runs" '
                "`ts` column to the selected UTC offset. `ts` is stored in UTC."
            ),
        )
        st.plotly_chart(
            dashboard_charts.hour_weekday_heatmap(
                heatmap_rows,
                tz_label=timezone_label,
            ),
            use_container_width=True,
            config=dict(_dashboard.PLOTLY_CONFIG),
        )
