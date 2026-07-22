# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-day throughput bar builder for the analytics dashboard.

Home of `done_per_day_bars` (the issues-resolved-per-day reliability strip)
and its private calendar-fill / series-shaping helpers.
`orchestrator.dashboard_charts` re-exports `done_per_day_bars` under its
original name so `dashboard_charts.done_per_day_bars` keeps resolving for
the widget pipeline. The shared no-data placeholder (`_empty_figure`) comes
from `orchestrator.dashboard_charts_base`, imported one-directionally.

Like the sibling chart modules, plotly is imported at module load: this
module is only reachable from the lazy `import dashboard_charts` inside
`orchestrator.dashboard.main` (which pulls it in via the compatibility
re-export), so the polling tick never imports it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator.analytics.read import ThroughputDayRow
from orchestrator.dashboard_charts_base import _empty_figure


_THROUGHPUT_CHART_HEIGHT = 150


@dataclass(frozen=True)
class _ThroughputSeries:
    days: Sequence[date]
    resolved: Sequence[int]


def _calendar_days(window_start: date, window_end: date) -> list[date]:
    days: list[date] = []
    current = window_start
    while current <= window_end:
        days.append(current)
        current = current + timedelta(days=1)
    return days


def _throughput_series(
    rows: Sequence[ThroughputDayRow],
    window_start: Optional[date],
    window_end: Optional[date],
) -> _ThroughputSeries:
    resolved_by_day = {row.day: int(row.resolved or 0) for row in rows}
    if window_start is not None and window_end is not None:
        days = _calendar_days(window_start, window_end)
    else:
        days = sorted(resolved_by_day)
    return _ThroughputSeries(
        days=days,
        resolved=[resolved_by_day.get(day, 0) for day in days],
    )


def done_per_day_bars(
    rows: Sequence[ThroughputDayRow],
    *,
    window_start: Optional[date] = None,
    window_end: Optional[date] = None,
    title: Optional[str] = None,
) -> go.Figure:
    """Issues-resolved-per-day bars for the reliability panel.

    Reads `ThroughputDayRow.resolved` per day. The SQL only returns
    days that actually carried a `done` / `rejected` `stage_enter`
    row, so a zero-resolved Tuesday in the middle of an otherwise-
    active week is silently absent from the data set. When callers
    pass `window_start` / `window_end` (both inclusive `date`s),
    every day in the window renders as an explicit zero bar -- the
    standalone mock draws the whole window so the operator can see
    the continuous baseline rather than a "gappy" set of mystery
    high-resolution days. Without the window the function falls
    back to the legacy behavior (one bar per SQL row only) so
    existing callers / tests keep working.
    """
    series = _throughput_series(rows, window_start, window_end)
    if not series.days:
        return _empty_figure(
            "No resolved issues in the current window.",
            height=_THROUGHPUT_CHART_HEIGHT,
        )
    fig = go.Figure(
        go.Bar(
            x=series.days,
            y=series.resolved,
            marker_color=theme.SUCCESS,
            hovertemplate="%{x}: %{y} resolved<extra></extra>",
        )
    )
    layout = theme.base_layout(title=title)
    top_margin = layout["margin"]["t"]
    layout["margin"] = {"l": 40, "r": 16, "t": top_margin, "b": 32}
    layout["yaxis"] = {
        **layout.get("yaxis", {}),
        "title": {"text": "resolved"},
    }
    # The throughput strip sits in the narrow reliability column;
    # at the 450px Plotly default it would dwarf the tiles above.
    # 150px matches the standalone mock's thin per-day strip.
    layout["height"] = _THROUGHPUT_CHART_HEIGHT
    fig.update_layout(**layout)
    return fig
