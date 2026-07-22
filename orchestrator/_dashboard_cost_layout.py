# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared layout and trace models for horizontal cost charts."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional, Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator.dashboard_charts_base import (
    _HORIZONTAL_BAR_EXTRA_HEIGHT,
    _HORIZONTAL_BAR_ROW_HEIGHT,
    _horizontal_panel_height,
    _money_text,
    _monospace_textfont,
)


HORIZONTAL_BAR_MARGIN = MappingProxyType({"l": 160, "r": 64, "b": 32})


@dataclass(frozen=True)
class _HorizontalCostLayout:
    row_count: int
    height: Optional[int] = None
    title: Optional[str] = None
    barmode: Optional[str] = None
    legend: Optional[dict[str, object]] = None
    row_height: int = _HORIZONTAL_BAR_ROW_HEIGHT
    extra_height: int = _HORIZONTAL_BAR_EXTRA_HEIGHT


def _apply_horizontal_cost_layout(
    figure: go.Figure,
    options: _HorizontalCostLayout,
) -> None:
    layout = theme.base_layout(title=options.title)
    if options.barmode is not None:
        layout["barmode"] = options.barmode
    if options.legend is not None:
        layout["legend"] = options.legend
    layout["margin"] = {
        **HORIZONTAL_BAR_MARGIN,
        "t": layout["margin"]["t"],
    }
    layout["height"] = _horizontal_panel_height(
        options.row_count,
        height=options.height,
        row_height=options.row_height,
        extra_height=options.extra_height,
    )
    figure.update_layout(**layout)
    figure.update_xaxes(
        title_text="USD",
        tickprefix="$",
        showline=False,
        zeroline=False,
    )
    figure.update_yaxes(automargin=True, showline=False, ticks="")


@dataclass(frozen=True)
class _CostBarTrace:
    name: str
    amounts: Sequence[float]
    y_ticks: Sequence[str]
    color: object
    hover_label: str
    offsetgroup: Optional[str] = None
    totals: Optional[Sequence[float]] = None


def _cost_bar_trace(options: _CostBarTrace) -> go.Bar:
    trace_kwargs = {
        "x": list(options.amounts),
        "y": list(options.y_ticks),
        "name": options.name,
        "orientation": "h",
        "marker_color": options.color,
        "cliponaxis": False,
        "hovertemplate": (
            f"%{{y}}<br>{options.hover_label}: $%{{x:,.2f}}<extra></extra>"
        ),
    }
    if options.offsetgroup is not None:
        trace_kwargs["offsetgroup"] = options.offsetgroup
    if options.totals is not None:
        trace_kwargs["text"] = _money_text(options.totals)
        trace_kwargs["textposition"] = "outside"
        trace_kwargs["textfont"] = _monospace_textfont()
    return go.Bar(**trace_kwargs)
