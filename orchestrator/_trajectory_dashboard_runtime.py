# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory-viewer page orchestration and Streamlit entrypoint."""
from __future__ import annotations

import html
from typing import Any, Sequence

from orchestrator import dashboard_theme as theme
from orchestrator import trajectory_reader
from orchestrator import _trajectory_dashboard_filters as filters_module
from orchestrator import _trajectory_dashboard_models as models
from orchestrator import _trajectory_dashboard_page as page_setup
from orchestrator import _trajectory_dashboard_picker as picker
from orchestrator._trajectory_dashboard_html import (
    _kpi_strip_html,
    _topbar_html,
)


def _render_trajectory_footer(
    st: Any,
    shown_count: int,
    page: models._TrajectoryPage,
) -> None:
    st.markdown(
        '<div class="orch-foot">'
        f"{theme.fmt_num(shown_count)} of {theme.fmt_num(page.total)} recorded "
        f"trajectories · reading {html.escape(str(page.log_path))}</div>",
        unsafe_allow_html=True,
    )


def _render_trajectory_page(
    st: Any,
    page: models._TrajectoryPage,
    filters: models._TrajectoryFilters,
    shown: Sequence[trajectory_reader.TrajectoryRun],
) -> None:
    st.markdown(_topbar_html(page.total, len(shown)), unsafe_allow_html=True)
    if page.total == 0:
        picker._render_no_trajectories(st, page.log_path)
        return
    st.markdown(
        _kpi_strip_html(trajectory_reader.summarize(shown)),
        unsafe_allow_html=True,
    )
    if not shown:
        st.info(page_setup.EMPTY_FILTER_MESSAGE)
        return
    picker._render_run_list(
        st,
        shown,
        page.fixture_total,
        filters.hide_fixtures,
    )
    picker._render_run_picker(st, shown)
    _render_trajectory_footer(st, len(shown), page)


def main() -> None:
    """Run the Streamlit trajectory viewer."""
    import streamlit as st

    page_setup._configure_page(st)
    page_setup._stop_if_unconfigured(st)
    page = page_setup._load_trajectory_page()
    filters = filters_module._render_trajectory_sidebar(st, page.options)
    shown = filters_module._filter_page_runs(page, filters)
    _render_trajectory_page(st, page, filters, shown)
