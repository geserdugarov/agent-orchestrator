# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Aligned token and cost axes for the dashboard usage chart."""
from __future__ import annotations

import math
from typing import Optional

from orchestrator import dashboard_theme as theme
from orchestrator import _dashboard_usage_data as usage_data
from orchestrator import _dashboard_usage_models as models


USAGE_GRID_STEPS = 5
USAGE_CHART_HEIGHT = 330


def _nice_axis_max(data_max: float, steps: int) -> float:
    """Return a rounded axis maximum divisible into equal steps."""
    if data_max <= 0 or steps <= 0:
        return float(max(steps, 1))
    rough_step = data_max / steps
    magnitude = 10 ** math.floor(math.log10(rough_step))
    normalized = rough_step / magnitude
    if normalized <= 1:
        nice_step = 1
    elif normalized <= 2:
        nice_step = 2
    elif normalized <= 5 / 2:
        nice_step = 5 / 2
    elif normalized <= 5:
        nice_step = 5
    else:
        nice_step = 10
    return nice_step * magnitude * steps


def _usage_axis_ranges(
    usage: models._UsageChartData,
    backend_rows_by_day: Optional[models.DailyTokenValues],
    mode: str,
) -> models._UsageAxisRanges:
    stack_totals = usage_data._usage_stack_totals(
        usage.days,
        usage.daily,
        backend_rows_by_day=backend_rows_by_day,
        mode=mode,
    )
    token_max = max(stack_totals, default=0)
    cost_max = max(
        (usage.daily[day][usage_data.COST_BAND] for day in usage.days),
        default=0,
    )
    return models._UsageAxisRanges(
        token_top=_nice_axis_max(token_max, USAGE_GRID_STEPS),
        cost_top=_nice_axis_max(cost_max, USAGE_GRID_STEPS),
    )


def _usage_layout(
    usage: models._UsageChartData,
    backend_rows_by_day: Optional[models.DailyTokenValues],
    mode: str,
    title: Optional[str],
) -> dict[str, object]:
    layout = theme.base_layout(title=title)
    ranges = _usage_axis_ranges(usage, backend_rows_by_day, mode)
    layout["yaxis"] = {
        **layout.get("yaxis", {}),
        "title": {"text": "tokens"},
        "range": [0, ranges.token_top],
        "dtick": ranges.token_top / USAGE_GRID_STEPS,
        "rangemode": "tozero",
        "showgrid": True,
    }
    layout["yaxis2"] = {
        "title": {"text": "USD"},
        "overlaying": "y",
        "side": "right",
        "range": [0, ranges.cost_top],
        "dtick": ranges.cost_top / USAGE_GRID_STEPS,
        "rangemode": "tozero",
        "gridcolor": theme.GRID,
        "linecolor": theme.GRID,
        "showgrid": False,
        "tickprefix": "$",
        "tickfont": {"color": theme.MUTED_TEXT},
    }
    layout["margin"] = {**layout.get("margin", {}), "t": 28}
    layout["hovermode"] = "x unified"
    layout["legend"] = {
        **layout.get("legend", {}),
        "orientation": "h",
        "yanchor": "bottom",
        "y": 1.02,
        "xanchor": "left",
        "x": 0,
    }
    layout["height"] = USAGE_CHART_HEIGHT
    return layout
