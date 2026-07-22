# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard token-usage card rendering."""
from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from orchestrator.dashboard_cards import _card_header_html


_TOKEN_TYPE_MODE = "type"


def _backend_tokens_by_day(
    backend_daily_rows: Sequence[Any],
) -> dict[date, dict[str, float]]:
    backend_by_day: dict[date, dict[str, float]] = {}
    for row in backend_daily_rows:
        by_backend = backend_by_day.setdefault(row.day, {})
        by_backend[row.backend] = (
            by_backend.get(row.backend, 0) + float(row.total_tokens or 0)
        )
    return backend_by_day


def _stack_mode_label(mode: str) -> str:
    return "By token type" if mode == _TOKEN_TYPE_MODE else "By backend"


def _stack_mode_index(mode: str) -> int:
    return 0 if mode == _TOKEN_TYPE_MODE else 1


def _render_hero_usage(
    *,
    st: Any,
    dashboard_charts: Any,
    ts_points: Any,
    backend_daily_rows: Any,
) -> None:
    """Render the hero spend and token-usage card."""
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
            st.session_state.stack_mode = _TOKEN_TYPE_MODE
        stack_mode = st.radio(
            "Stack mode",
            options=(_TOKEN_TYPE_MODE, "backend"),
            format_func=_stack_mode_label,
            index=_stack_mode_index(st.session_state.stack_mode),
            horizontal=True,
            label_visibility="collapsed",
            key="_stack_mode_radio",
        )
        st.session_state.stack_mode = stack_mode
        backend_by_day = (
            _backend_tokens_by_day(backend_daily_rows)
            if stack_mode == "backend"
            else None
        )
        st.plotly_chart(
            dashboard_charts.usage_over_time(
                ts_points,
                backend_rows_by_day=backend_by_day,
                mode=stack_mode,
                title=None,
            ),
            use_container_width=True,
            config=dict(_dashboard.PLOTLY_CONFIG),
        )
