# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Generic horizontal cost-bar data and figure construction."""
from __future__ import annotations

from dataclasses import dataclass
from inspect import Parameter, Signature
from typing import Any, Optional, Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator import _dashboard_cost_layout as cost_layout
from orchestrator.dashboard_charts_base import (
    _empty_figure,
    _money_text,
    _monospace_textfont,
    _reverse_lists,
    _two_line_y_ticks,
)


DEFAULT_CHART_HEIGHT = 120


@dataclass(frozen=True)
class _HorizontalBars:
    labels: Sequence[str]
    subs: Sequence[str]
    costs: Sequence[float]
    colors: Sequence[str]


@dataclass(frozen=True)
class _HorizontalBarRequest:
    rows: Sequence[tuple[str, str, float, str]]
    title: Optional[str]
    accent: Optional[str]
    preserve_order: bool
    height: Optional[int]


def _reverse_horizontal_bars(bars: _HorizontalBars) -> _HorizontalBars:
    reversed_values = _reverse_lists(
        bars.labels,
        bars.subs,
        bars.costs,
        bars.colors,
    )
    return _HorizontalBars(*reversed_values)


def _horizontal_bars_data(
    rows: Sequence[tuple[str, str, float, str]],
    accent: Optional[str],
    preserve_order: bool,
) -> _HorizontalBars:
    ordered = list(rows)
    if not preserve_order:
        ordered.sort(key=_cost_item_sort_key)
    return _reverse_horizontal_bars(
        _HorizontalBars(
            labels=[row[0] for row in ordered],
            subs=[row[1] for row in ordered],
            costs=[float(row[2] or 0) for row in ordered],
            colors=[row[3] or accent or theme.ACCENT for row in ordered],
        )
    )


def _cost_item_sort_key(row: tuple[str, str, float, str]) -> float:
    return -float(row[2] or 0)


def cost_horizontal_bars(*args: Any, **kwargs: Any) -> go.Figure:
    """Render generic horizontal cost bars through the stable call shape."""
    bound = _HORIZONTAL_BAR_SIGNATURE.bind(*args, **kwargs)
    bound.apply_defaults()
    request = _HorizontalBarRequest(
        rows=bound.arguments["items"],
        title=bound.arguments["title"],
        accent=bound.arguments["accent"],
        preserve_order=bound.arguments["preserve_order"],
        height=bound.arguments["height"],
    )
    if not request.rows:
        return _empty_figure(
            "No data matches the current filters.",
            height=request.height or DEFAULT_CHART_HEIGHT,
        )
    bars = _horizontal_bars_data(
        request.rows,
        request.accent,
        request.preserve_order,
    )
    figure = go.Figure(
        go.Bar(
            x=bars.costs,
            y=_two_line_y_ticks(bars.labels, bars.subs),
            orientation="h",
            marker_color=bars.colors,
            text=_money_text(bars.costs),
            textposition="outside",
            textfont=_monospace_textfont(),
            cliponaxis=False,
            hovertemplate="%{y}: $%{x:,.2f}<extra></extra>",
        )
    )
    cost_layout._apply_horizontal_cost_layout(
        figure,
        cost_layout._HorizontalCostLayout(
            title=request.title,
            row_count=len(bars.costs),
            height=request.height,
        ),
    )
    return figure


_HORIZONTAL_BAR_SIGNATURE = Signature(
    (
        Parameter("items", Parameter.POSITIONAL_OR_KEYWORD),
        Parameter("title", Parameter.KEYWORD_ONLY, default=None),
        Parameter("accent", Parameter.KEYWORD_ONLY, default=None),
        Parameter("preserve_order", Parameter.KEYWORD_ONLY, default=False),
        Parameter("height", Parameter.KEYWORD_ONLY, default=None),
    ),
)
cost_horizontal_bars.__signature__ = _HORIZONTAL_BAR_SIGNATURE
