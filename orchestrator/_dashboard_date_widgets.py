# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard date-filter widget primitives."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator import dashboard_state


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
    choices = (
        dashboard_state.PRESET_RECENT_THREE_DAYS,
        dashboard_state.PRESET_RECENT_WEEK,
        dashboard_state.PRESET_ALL,
    )
    if preset not in choices:
        return len(choices) - 1
    return choices.index(preset)


def _render_preset_choice(st: Any, column: Any) -> str:
    options = (
        dashboard_state.PRESET_RECENT_THREE_DAYS,
        dashboard_state.PRESET_RECENT_WEEK,
        dashboard_state.PRESET_ALL,
    )
    with column:
        return st.radio(
            "Range preset",
            options=options,
            format_func=lambda preset: dashboard_state.PRESET_INLINE_LABELS[preset],
            index=_preset_radio_index(st.session_state.preset),
            horizontal=True,
            label_visibility="collapsed",
            key="_preset_radio",
        )
