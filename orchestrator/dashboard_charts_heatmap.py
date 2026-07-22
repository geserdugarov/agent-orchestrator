# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Weekday-by-hour activity heatmap builder for the analytics dashboard.

Home of `hour_weekday_heatmap` (the 7x24 token-volume heatmap) and its
private matrix / layout / point-validation helpers plus the weekday-label
and hours-per-day grid constants. `orchestrator.dashboard_charts` re-exports
`hour_weekday_heatmap` under its original name so
`dashboard_charts.hour_weekday_heatmap` keeps resolving for the widget
pipeline.

Like the sibling chart modules, plotly is imported at module load: this
module is only reachable from the lazy `import dashboard_charts` inside
`orchestrator.dashboard.main` (which pulls it in via the compatibility
re-export), so the polling tick never imports it.
"""
from __future__ import annotations

from typing import Optional, Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator.analytics.read import HourlyHeatmapPoint

# Postgres `EXTRACT(DOW FROM ts)` is 0 = Sunday; the standalone mock's
# heatmap renders Sunday-first, so we keep that ordering here too --
# the chart label row drives what the operator reads off the y-axis.
_WEEKDAY_LABELS: tuple[str, ...] = (
    "Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat",
)

# The activity heatmap is a fixed 7-weekday-row x 24-hour-column grid. The
# row count follows `_WEEKDAY_LABELS` so the matrix and the y-axis labels
# cannot drift apart; the column span is the hours in a day.
_HOURS_PER_DAY = 24


def _valid_heatmap_point(point: HourlyHeatmapPoint, weekdays: int) -> bool:
    valid_weekday = 0 <= int(point.weekday) < weekdays
    valid_hour = 0 <= int(point.hour) < _HOURS_PER_DAY
    return valid_weekday and valid_hour


def _heatmap_matrix(
    points: Sequence[HourlyHeatmapPoint],
) -> list[list[int]]:
    weekdays = len(_WEEKDAY_LABELS)
    matrix = [
        [0 for _ in range(_HOURS_PER_DAY)] for _ in range(weekdays)
    ]
    for point in points:
        if _valid_heatmap_point(point, weekdays):
            matrix[int(point.weekday)][int(point.hour)] = int(
                getattr(point, "total_tokens", 0) or 0
            )
    return matrix


def _heatmap_layout(title: Optional[str]) -> dict[str, object]:
    layout = theme.base_layout(title=title)
    top_margin = layout["margin"]["t"]
    layout["margin"] = {"l": 48, "r": 24, "t": top_margin, "b": 32}
    layout["height"] = 240
    layout["plot_bgcolor"] = theme.BORDER
    return layout


def hour_weekday_heatmap(
    points: Sequence[HourlyHeatmapPoint],
    *,
    title: Optional[str] = None,
    tz_label: str = "UTC",
) -> go.Figure:
    """7x24 weekday-by-hour token-volume heatmap.

    Postgres `EXTRACT(DOW FROM ts)` is 0 = Sunday, which is also the
    standalone mock's row ordering, so we render the matrix Sunday-
    first without re-mapping the weekday axis. Cell values are
    total token volume (`input + output + cache_read + cache_write`)
    in that (weekday, hour) cell -- matching the standalone mock's
    "Token volume by hour x weekday" framing rather than raw event
    counts, which would over-weight the cheap `stage_enter` /
    `stage_evaluation` cells against the agent-exit rows that
    actually drive spend. `HourlyHeatmapPoint.count` stays available
    for callers that want the event count, but the heatmap renders
    `total_tokens`. `tz_label` only annotates the x-axis -- the
    caller is responsible for passing the matching offset to
    `get_hourly_heatmap` so the cells already reflect that zone.
    """
    fig = go.Figure(
        go.Heatmap(
            z=_heatmap_matrix(points),
            x=[format(hour, "02d") for hour in range(_HOURS_PER_DAY)],
            y=list(_WEEKDAY_LABELS),
            colorscale=[
                [0, theme.CARD_BG],
                [0.05, "#eae8fb"],
                [1.0, theme.ACCENT],
            ],
            showscale=False,
            xgap=2,
            ygap=2,
            hovertemplate="%{y} %{x}:00 -- %{z:,} tokens<extra></extra>",
        )
    )
    # 7 rows x 24 columns: ~240px keeps the cells close to the
    # mock's compact squares instead of stretching them into tall
    # rectangles at the default 450px.
    # Paint the plot background the border colour so the `xgap`/`ygap`
    # between cells reads as a weekday x hour grid. Zero-volume cells
    # are white (colorscale[0] == CARD_BG), so without a contrasting
    # backdrop the gaps vanish and the sparse right-hand hours look
    # like missing data rather than empty cells.
    fig.update_layout(**_heatmap_layout(title))
    fig.update_xaxes(
        title_text=f"hour ({tz_label})", type="category", showgrid=False,
    )
    fig.update_yaxes(title_text="", autorange="reversed", showgrid=False)
    if not points:
        fig.add_annotation(
            text="No events match the current filters.",
            x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
            font={"color": theme.MUTED_TEXT, "size": theme.FONT_SIZE},
        )
    return fig
