# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Public dashboard usage-chart builders."""
from __future__ import annotations

from typing import Optional, Sequence

from plotly import graph_objects as go

from orchestrator import _dashboard_usage_axis as usage_axis
from orchestrator import _dashboard_usage_models as models
from orchestrator import _dashboard_usage_traces as traces
from orchestrator.analytics.read import BackendEfficiencyRow, TimeSeriesPoint
from orchestrator.dashboard_charts_base import _empty_figure


def usage_over_time(
    points: Sequence[TimeSeriesPoint],
    *,
    backend_rows_by_day: Optional[models.DailyTokenValues] = None,
    mode: str = "type",
    title: Optional[str] = "Spend & token usage over time",
) -> go.Figure:
    """Build stacked daily token usage with a cost-line overlay."""
    usage = traces._prepare_usage_data(points, backend_rows_by_day, mode)
    if usage is None:
        return _empty_figure(
            "No events match the current filters.",
            height=usage_axis.USAGE_CHART_HEIGHT,
        )
    figure = go.Figure()
    traces._add_usage_stack_traces(
        figure,
        usage,
        backend_rows_by_day,
        mode,
    )
    traces._add_usage_cost_trace(figure, usage)
    figure.update_layout(
        **usage_axis._usage_layout(
            usage,
            backend_rows_by_day,
            mode,
            title,
        )
    )
    return figure


def backend_per_day(
    rows: Sequence[BackendEfficiencyRow],
) -> dict[str, dict[str, float]]:
    """Keep the historical placeholder for a future backend-day aggregate."""
    return {}
