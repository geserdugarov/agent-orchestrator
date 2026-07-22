# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trace construction and usage-data preparation."""
from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator import _dashboard_usage_data as usage_data
from orchestrator import _dashboard_usage_models as models
from orchestrator.analytics.read import TimeSeriesPoint


_COLOR_KEY = "color"


def _add_token_stack_trace(
    figure: go.Figure,
    *,
    days: Sequence[date],
    token_series: Sequence[float],
    name: str,
    color: str,
) -> None:
    figure.add_trace(
        go.Scatter(
            x=usage_data._date_axis(days),
            y=list(token_series),
            name=name,
            mode="lines",
            stackgroup="tokens",
            line={"width": 0.5, _COLOR_KEY: color},
            fillcolor=color,
            hovertemplate=f"%{{x}}<br>{name}: %{{y:,}} tokens<extra></extra>",
        )
    )


def _prepare_usage_data(
    points: Sequence[TimeSeriesPoint],
    backend_rows_by_day: Optional[models.DailyTokenValues],
    mode: str,
) -> Optional[models._UsageChartData]:
    if not points and not backend_rows_by_day:
        return None
    daily = usage_data._roll_up_time_series(points)
    if mode == usage_data.BACKEND_MODE and backend_rows_by_day:
        usage_data._ensure_backend_days(daily, backend_rows_by_day)
    days = sorted(daily)
    if not days:
        return None
    return models._UsageChartData(daily=daily, days=days)


def _add_backend_usage_traces(
    figure: go.Figure,
    usage: models._UsageChartData,
    backend_rows_by_day: models.DailyTokenValues,
) -> None:
    backends = usage_data._backend_names(backend_rows_by_day)
    for backend in backends:
        backend_color = theme.color_for(
            backend,
            backends,
            explicit=theme.BACKEND_COLORS,
        )
        _add_token_stack_trace(
            figure,
            days=usage.days,
            token_series=[
                backend_rows_by_day.get(day, {}).get(backend, 0)
                for day in usage.days
            ],
            name=backend,
            color=backend_color,
        )


def _add_token_type_usage_traces(
    figure: go.Figure,
    usage: models._UsageChartData,
) -> None:
    for band, label in (
        (usage_data.INPUT_BAND, "Input"),
        (usage_data.OUTPUT_BAND, "Output"),
        (usage_data.CACHE_BAND, "Cache"),
    ):
        _add_token_stack_trace(
            figure,
            days=usage.days,
            token_series=[usage.daily[day][band] for day in usage.days],
            name=label,
            color=theme.TOKEN_TYPE_COLORS[label],
        )


def _add_usage_stack_traces(
    figure: go.Figure,
    usage: models._UsageChartData,
    backend_rows_by_day: Optional[models.DailyTokenValues],
    mode: str,
) -> None:
    if mode == usage_data.BACKEND_MODE and backend_rows_by_day:
        _add_backend_usage_traces(figure, usage, backend_rows_by_day)
        return
    _add_token_type_usage_traces(figure, usage)


def _add_usage_cost_trace(
    figure: go.Figure,
    usage: models._UsageChartData,
) -> None:
    figure.add_trace(
        go.Scatter(
            x=usage_data._date_axis(usage.days),
            y=[usage.daily[day][usage_data.COST_BAND] for day in usage.days],
            name="Cost",
            mode="lines+markers",
            line={_COLOR_KEY: theme.INK, "width": 2},
            marker={"size": 5, _COLOR_KEY: theme.INK},
            yaxis="y2",
            hovertemplate="%{x}<br>Cost: $%{y:.2f}<extra></extra>",
        )
    )
