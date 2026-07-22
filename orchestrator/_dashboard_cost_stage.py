# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-stage cost-bar data and figure construction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator import _dashboard_cost_horizontal as horizontal
from orchestrator import _dashboard_cost_layout as cost_layout
from orchestrator.analytics.read import StageBreakdown
from orchestrator.dashboard_charts_base import (
    _empty_figure,
    _horizontal_legend,
    _reverse_lists,
    _two_line_y_ticks,
)


CACHE_LIGHTEN = 0.45
HEX_BASE = 16


@dataclass(frozen=True)
class _StageCostBars:
    labels: Sequence[str]
    subs: Sequence[str]
    no_cache: Sequence[float]
    cache: Sequence[float]
    totals: Sequence[float]
    colors: Sequence[str]
    cache_colors: Sequence[str]


def _stage_no_cache_cost(row: StageBreakdown) -> float:
    no_cache = float(row.no_cache_cost_usd or 0)
    cache = float(row.cache_cost_usd or 0)
    total = float(row.total_cost_usd or 0)
    if no_cache == 0 and cache == 0 and total > 0:
        return total
    return no_cache


def _reverse_stage_cost_bars(bars: _StageCostBars) -> _StageCostBars:
    reversed_values = _reverse_lists(
        bars.labels,
        bars.subs,
        bars.no_cache,
        bars.cache,
        bars.totals,
        bars.colors,
        bars.cache_colors,
    )
    return _StageCostBars(*reversed_values)


def _stage_cost_bars(rows: Sequence[StageBreakdown]) -> _StageCostBars:
    ordered = sorted(rows, key=_stage_cost_sort_key)
    colors = [
        theme.color_for(row.stage, explicit=theme.STAGE_COLORS)
        for row in ordered
    ]
    return _reverse_stage_cost_bars(
        _StageCostBars(
            labels=[row.stage for row in ordered],
            subs=[f"{int(row.runs or 0):,} runs" for row in ordered],
            no_cache=[_stage_no_cache_cost(row) for row in ordered],
            cache=[float(row.cache_cost_usd or 0) for row in ordered],
            totals=[float(row.total_cost_usd or 0) for row in ordered],
            colors=colors,
            cache_colors=[_lighten_hex(color, CACHE_LIGHTEN) for color in colors],
        )
    )


def _stage_cost_sort_key(row: StageBreakdown) -> float:
    return -float(row.total_cost_usd or 0)


def cost_by_stage(
    rows: Sequence[StageBreakdown],
    *,
    height: Optional[int] = None,
) -> go.Figure:
    """Build stacked cache and no-cache cost bars per workflow stage."""
    if not rows:
        return _empty_figure(
            "No stage data matches the current filters.",
            height=height or horizontal.DEFAULT_CHART_HEIGHT,
        )
    bars = _stage_cost_bars(rows)
    y_ticks = _two_line_y_ticks(bars.labels, bars.subs)
    figure = go.Figure()
    figure.add_trace(
        cost_layout._cost_bar_trace(
            cost_layout._CostBarTrace(
                name="No cache",
                amounts=bars.no_cache,
                y_ticks=y_ticks,
                color=bars.colors,
                hover_label="No cache",
            )
        )
    )
    figure.add_trace(
        cost_layout._cost_bar_trace(
            cost_layout._CostBarTrace(
                name="Cache",
                amounts=bars.cache,
                y_ticks=y_ticks,
                color=bars.cache_colors,
                hover_label="Cache",
                totals=bars.totals,
            )
        )
    )
    cost_layout._apply_horizontal_cost_layout(
        figure,
        cost_layout._HorizontalCostLayout(
            row_count=len(y_ticks),
            height=height,
            barmode="stack",
            legend=_horizontal_legend(),
        ),
    )
    return figure


def _lighten_hex(hex_color: str, alpha: float) -> str:
    hex_digits = hex_color.lstrip("#")
    red = int(hex_digits[:2], HEX_BASE)
    green = int(hex_digits[2:4], HEX_BASE)
    blue = int(hex_digits[4:6], HEX_BASE)
    return f"rgba({red},{green},{blue},{alpha:.2f})"
