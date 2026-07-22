# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard date-window selection and range bar."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from orchestrator import _dashboard_date_widgets as date_widgets
from orchestrator import dashboard_state


def _initial_filter_window(
    preset_choice: str,
    extent: Any,
    extent_min_d: date,
    extent_max_d: date,
) -> dashboard_state.DateWindow:
    return (
        dashboard_state.preset_window(preset_choice, extent)
        or dashboard_state.to_window(extent_min_d, extent_max_d)
    )


def _render_date_inputs(
    st: Any,
    columns: date_widgets._DateFilterColumns,
    initial_window: dashboard_state.DateWindow,
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
    extent: Any,
    extent_min_d: date,
    extent_max_d: date,
) -> tuple[dashboard_state.DateWindow, Any]:
    """Render the preset and date-range controls."""
    if "preset" not in st.session_state:
        st.session_state.preset = dashboard_state.DEFAULT_PRESET
    with st.container(border=True):
        st.markdown(
            '<div class="orch-cardmark"></div>',
            unsafe_allow_html=True,
        )
        columns = date_widgets._date_filter_columns(st)
        date_widgets._render_date_filter_label(st, columns.label)
        preset_choice = date_widgets._render_preset_choice(st, columns.preset)
        initial_window = _initial_filter_window(
            preset_choice,
            extent,
            extent_min_d,
            extent_max_d,
        )
        dates = _render_date_inputs(
            st,
            columns,
            initial_window,
            extent_min_d,
            extent_max_d,
        )
        with columns.meta:
            meta_slot = st.empty()
    st.session_state.preset = preset_choice
    return dashboard_state.to_window(*dates), meta_slot
