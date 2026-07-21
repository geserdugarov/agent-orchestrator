# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Usage-over-time Plotly figure builders for the analytics dashboard.

Home of the hero chart family: `usage_over_time` (stacked daily token
consumption with a cost-line overlay, in token-type or per-backend stack
mode) and the `backend_per_day` API stub that feeds its per-backend stack,
plus the private roll-up / trace / axis / layout helpers and the token-band
constants they use. `orchestrator.dashboard_charts` re-exports the two public
builders under their original names so `dashboard_charts.usage_over_time` /
`dashboard_charts.backend_per_day` keep resolving for the widget pipeline.

The shared low-level chart primitives (`_empty_figure` and the axis / money /
textfont helpers) live in `orchestrator.dashboard_charts_base`; this module
imports the one it needs from that base, not from `dashboard_charts`, so the
dependency runs one way and a direct import of this module is cycle-free.

Like the sibling chart modules, plotly is imported at module load: this
module is only reachable from the lazy `import dashboard_charts` inside
`orchestrator.dashboard.main` (which pulls it in via the compatibility
re-export), so the polling tick never imports it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator.analytics.read import (
    BackendEfficiencyRow,
    TimeSeriesPoint,
)
from orchestrator.dashboard_charts_base import _empty_figure

_DailyTokenValues = dict[date, dict[str, float]]

# Number of equal gridline steps the twin token / cost y-axes are split into
# so a tokens gridline and its USD counterpart land on the same pixel row.
_USAGE_GRID_STEPS = 5


# Token-usage band identifiers, shared by the daily accumulators and the
# stacked-area series builders.
_INPUT = "input"
_OUTPUT = "output"
_CACHE = "cache"
_COST = "cost"


def _empty_token_bucket() -> dict[str, float]:
    """A fresh per-day accumulator with every token/cost band zeroed."""
    return {_INPUT: 0.0, _OUTPUT: 0.0, _CACHE: 0.0, _COST: 0.0}


def _date_axis(days: Sequence[date]) -> list:
    """Return `days` as date objects; Plotly handles ISO formatting."""
    return list(days)


def _nice_axis_max(data_max: float, steps: int) -> float:
    """Smallest "nice" axis maximum >= `data_max`, divisible into `steps`
    equal round increments.

    Picks a step size off the 1 / 2 / 2.5 / 5 / 10 x 10ⁿ ladder so the
    axis tops out just above the data while every tick stays a round
    number. Returning ``step_size * steps`` (rather than the raw
    maximum) lets two independent axes share the exact same fractional
    tick positions: divide each into the same `steps` and a gridline on
    one axis lands on the same pixel row as the matching tick on the
    other. Non-positive input yields `max(steps, 1)` so a flat / empty
    series still draws `steps` unit-high gridlines instead of a zero-
    height axis.
    """
    if data_max <= 0 or steps <= 0:
        return float(max(steps, 1))
    rough = data_max / steps
    mag = 10 ** math.floor(math.log10(rough))
    norm = rough / mag
    if norm <= 1:
        nice = 1.0
    elif norm <= 2:
        nice = 2.0
    elif norm <= 2.5:
        nice = 2.5
    elif norm <= 5:
        nice = 5.0
    else:
        nice = 10.0
    return nice * mag * steps


def _add_token_stack_trace(
    fig: go.Figure,
    *,
    days: Sequence[date],
    token_series: Sequence[float],
    name: str,
    color: str,
) -> None:
    fig.add_trace(
        go.Scatter(
            x=_date_axis(days),
            y=list(token_series),
            name=name,
            mode="lines",
            stackgroup="tokens",
            line={"width": 0.5, "color": color},
            fillcolor=color,
            hovertemplate=(
                f"%{{x}}<br>{name}: %{{y:,}} tokens<extra></extra>"
            ),
        )
    )


def _roll_up_time_series(
    points: Sequence[TimeSeriesPoint],
) -> _DailyTokenValues:
    daily: _DailyTokenValues = {}
    for point in points:
        bucket = daily.setdefault(
            point.day,
            _empty_token_bucket(),
        )
        bucket[_INPUT] += float(point.input_tokens or 0)
        bucket[_OUTPUT] += float(point.output_tokens or 0)
        # Total cache band: cache_read + cache_write -- matching the
        # standalone mock's `r.cr + r.cw` accounting. `cached_tokens`
        # (the cumulative cached count) is deliberately excluded so
        # we do not double-count the same prompt slices.
        bucket[_CACHE] += float(
            (point.cache_read_tokens or 0) + (point.cache_write_tokens or 0)
        )
        bucket[_COST] += float(point.cost_usd or 0)
    return daily


def _ensure_backend_days(
    daily: _DailyTokenValues,
    backend_rows_by_day: _DailyTokenValues,
) -> None:
    for day in backend_rows_by_day:
        daily.setdefault(
            day,
            _empty_token_bucket(),
        )


def _backend_names(
    backend_rows_by_day: _DailyTokenValues,
) -> list[str]:
    return sorted({
        backend
        for by_backend in backend_rows_by_day.values()
        for backend in by_backend
    })


def _usage_stack_totals(
    days: Sequence[date],
    daily: _DailyTokenValues,
    *,
    backend_rows_by_day: Optional[_DailyTokenValues],
    mode: str,
) -> list[float]:
    if mode == "backend" and backend_rows_by_day:
        return [sum(backend_rows_by_day.get(day, {}).values()) for day in days]
    return [_daily_token_total(daily[day]) for day in days]


def _daily_token_total(bucket: dict[str, float]) -> float:
    return sum(bucket[token_type] for token_type in (_INPUT, _OUTPUT, _CACHE))


@dataclass(frozen=True)
class _UsageChartData:
    daily: _DailyTokenValues
    days: Sequence[date]


@dataclass(frozen=True)
class _UsageAxisRanges:
    token_top: float
    cost_top: float


def _prepare_usage_data(
    points: Sequence[TimeSeriesPoint],
    backend_rows_by_day: Optional[_DailyTokenValues],
    mode: str,
) -> Optional[_UsageChartData]:
    if not points and not backend_rows_by_day:
        return None
    daily = _roll_up_time_series(points)
    if mode == "backend" and backend_rows_by_day:
        _ensure_backend_days(daily, backend_rows_by_day)
    days = sorted(daily)
    if not days:
        return None
    return _UsageChartData(daily=daily, days=days)


def _add_backend_usage_traces(
    fig: go.Figure,
    usage: _UsageChartData,
    backend_rows_by_day: _DailyTokenValues,
) -> None:
    backends = _backend_names(backend_rows_by_day)
    for backend in backends:
        color = theme.color_for(
            backend, backends, explicit=theme.BACKEND_COLORS,
        )
        _add_token_stack_trace(
            fig,
            days=usage.days,
            token_series=[
                backend_rows_by_day.get(day, {}).get(backend, 0)
                for day in usage.days
            ],
            name=backend,
            color=color,
        )


def _add_token_type_usage_traces(
    fig: go.Figure,
    usage: _UsageChartData,
) -> None:
    for band, label in (
        (_INPUT, "Input"),
        (_OUTPUT, "Output"),
        (_CACHE, "Cache"),
    ):
        _add_token_stack_trace(
            fig,
            days=usage.days,
            token_series=[usage.daily[day][band] for day in usage.days],
            name=label,
            color=theme.TOKEN_TYPE_COLORS[label],
        )


def _add_usage_stack_traces(
    fig: go.Figure,
    usage: _UsageChartData,
    backend_rows_by_day: Optional[_DailyTokenValues],
    mode: str,
) -> None:
    if mode == "backend" and backend_rows_by_day:
        _add_backend_usage_traces(fig, usage, backend_rows_by_day)
        return
    _add_token_type_usage_traces(fig, usage)


def _add_usage_cost_trace(fig: go.Figure, usage: _UsageChartData) -> None:
    fig.add_trace(
        go.Scatter(
            x=_date_axis(usage.days),
            y=[usage.daily[day][_COST] for day in usage.days],
            name="Cost",
            mode="lines+markers",
            line={"color": theme.INK, "width": 2},
            marker={"size": 5, "color": theme.INK},
            yaxis="y2",
            hovertemplate="%{x}<br>Cost: $%{y:.2f}<extra></extra>",
        )
    )


def _usage_axis_ranges(
    usage: _UsageChartData,
    backend_rows_by_day: Optional[_DailyTokenValues],
    mode: str,
) -> _UsageAxisRanges:
    stack_totals = _usage_stack_totals(
        usage.days,
        usage.daily,
        backend_rows_by_day=backend_rows_by_day,
        mode=mode,
    )
    token_max = max(stack_totals, default=0)
    cost_max = max(
        (usage.daily[day][_COST] for day in usage.days),
        default=0,
    )
    return _UsageAxisRanges(
        token_top=_nice_axis_max(token_max, _USAGE_GRID_STEPS),
        cost_top=_nice_axis_max(cost_max, _USAGE_GRID_STEPS),
    )


def _usage_layout(
    usage: _UsageChartData,
    backend_rows_by_day: Optional[_DailyTokenValues],
    mode: str,
    title: Optional[str],
) -> dict[str, object]:
    layout = theme.base_layout(title=title)
    ranges = _usage_axis_ranges(usage, backend_rows_by_day, mode)
    layout["yaxis"] = {
        **layout.get("yaxis", {}),
        "title": {"text": "tokens"},
        "range": [0, ranges.token_top],
        "dtick": ranges.token_top / _USAGE_GRID_STEPS,
        "rangemode": "tozero",
        "showgrid": True,
    }
    layout["yaxis2"] = {
        "title": {"text": "USD"},
        "overlaying": "y",
        "side": "right",
        "range": [0, ranges.cost_top],
        "dtick": ranges.cost_top / _USAGE_GRID_STEPS,
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
        "yanchor": "bottom", "y": 1.02,
        "xanchor": "left", "x": 0,
    }
    layout["height"] = 330
    return layout


def usage_over_time(
    points: Sequence[TimeSeriesPoint],
    *,
    backend_rows_by_day: Optional[_DailyTokenValues] = None,
    mode: str = "type",
    title: Optional[str] = "Spend & token usage over time",
) -> go.Figure:
    """Hero chart: stacked daily token usage with a cost line overlay.

    `points` is the time-series read-model shape (one row per
    `(day, event, count, cost_usd, input_tokens, output_tokens)`). The
    builder rolls up per-day totals across every event in the window
    so the chart still aggregates correctly when the operator narrows
    the event multiselect to a subset.

    Two stack modes match the standalone mock's segmented control:

    - ``"type"`` (default) stacks daily input / output / cache
      token volumes. The read model's per-day query sums
      `input_tokens`, `output_tokens`, `cache_read_tokens`, and
      `cache_write_tokens` for every `agent_exit` row in the cell --
      mirroring the headline KPI's accounting -- so the Cache band
      reflects the same volume the "Total tokens" tile counts
      instead of dropping cache tokens on the floor.
    - ``"backend"`` stacks per-backend daily token volumes. Caller
      passes `backend_rows_by_day` -- a `{day: {backend: tokens}}`
      mapping derived from `get_recent_agent_exits` (it carries the
      per-row `backend` and token counts together) or any equivalent
      aggregate. Without that mapping the builder falls back to the
      token-type stack.

    The dashed black line overlay carries daily cost on a secondary
    y-axis so the operator can read spend and usage off the same
    chart. Cost ticks render in `$1.2K` shorthand via
    `dashboard_theme.fmt_money`.
    """
    usage = _prepare_usage_data(points, backend_rows_by_day, mode)
    if usage is None:
        return _empty_figure(
            "No events match the current filters.", height=330,
        )

    fig = go.Figure()
    _add_usage_stack_traces(fig, usage, backend_rows_by_day, mode)
    _add_usage_cost_trace(fig, usage)
    # Align the two y-axes on a shared set of horizontal gridlines.
    # Both axes start at zero and split into the same number of equal
    # steps, so each tokens gridline and its USD counterpart sit on the
    # same pixel row. Without this Plotly picks independent "nice"
    # ranges (e.g. 6 token lines from 0 to 1B vs 5 USD lines to $1000)
    # whose gridlines visually drift apart. Only the left (tokens) axis
    # draws the gridlines; the right (USD) axis ticks land on them.
    fig.update_layout(**_usage_layout(usage, backend_rows_by_day, mode, title))
    return fig


def backend_per_day(
    rows: Sequence[BackendEfficiencyRow],
) -> dict[str, dict[str, float]]:
    """Stub helper kept for the API: the dashboard caller assembles
    the per-day backend token table from `get_recent_agent_exits` so
    `usage_over_time` can stack the right column. Returns an empty
    mapping; the dashboard uses the more granular agent-exit rows.

    Kept exported so future code can hook the per-backend stack to
    a future read-model aggregate without re-plumbing the chart.
    """
    return {}
