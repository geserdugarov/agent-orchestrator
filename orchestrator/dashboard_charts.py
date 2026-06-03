# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Plotly figure builders for the analytics dashboard.

Pure functions: each builder takes already-fetched read-model rows
(or raw timestamps for the 7x24 heatmap) and returns a
``plotly.graph_objects.Figure``. The dashboard layer is responsible
for the query + sidebar filters and for handing the resulting
``Figure`` to ``st.plotly_chart``; this module does no IO and no
Streamlit calls.

Plotly is imported at module load here because this module is only
reachable from the lazy ``import`` inside ``orchestrator.dashboard.main``
(see the lazy-import guard in ``tests/test_dashboard.py``). The
orchestrator polling tick must not import this module, and
``orchestrator/dashboard.py`` must not import it at module load --
both invariants are enforced by tests.

The shared theme tokens (colors, font, layout defaults) live in
``orchestrator.dashboard_theme``; that module is deliberately
plotly-free so the dashboard chrome can pull semantic colors without
forcing the optional ``dashboard`` dependency group onto every caller.

The chart shapes mirror what the parent dashboard rewrite (#317)
needs:

- ``usage_over_time`` -- stacked-bar of events per day.
- ``stage_bars`` -- count per workflow stage.
- ``review_round_bars`` -- count of ``agent_exit`` rows per
  ``review_round`` value.
- ``repo_bars`` -- per-repo issue / cost totals from the issues
  overview rows.
- ``cost_coverage`` -- donut of ``cost_source`` distribution across
  ``agent_exit`` rows.
- ``throughput`` -- daily total of all events.
- ``heatmap_7x24`` -- weekday-by-hour heat map of activity counts.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Sequence

import plotly.graph_objects as go

from . import dashboard_theme as theme
from .analytics.read import (
    AgentExitRow,
    IssueSummaryRow,
    StageBreakdown,
    TimeSeriesPoint,
)

# Monday-first ordering matches Python's `datetime.weekday()` and is
# what the rest of the orchestrator's analytics queries assume.
_WEEKDAY_LABELS: tuple[str, ...] = (
    "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
)


def _empty_figure(message: str) -> go.Figure:
    """Return a placeholder figure with a centered annotation.

    Plotly raises no error on an empty data series, but the default
    "blank canvas" is a confusing empty-state. Every builder routes
    its no-data branch through here so the user sees a single
    consistent "nothing matches" label across charts.
    """
    fig = go.Figure()
    fig.update_layout(**theme.base_layout())
    fig.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"color": theme.MUTED_TEXT, "size": theme.FONT_SIZE},
    )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def usage_over_time(
    points: Sequence[TimeSeriesPoint],
    *,
    title: str = "Events over time",
) -> go.Figure:
    """Daily stacked-bar chart of events.

    `points` is the read-model `(day, event, count)` shape; this
    builder pivots it into one bar trace per event so the legend
    drives event-level visibility. Days with zero events stay
    invisible -- the SQL aggregate elides them, so a sparse window
    renders as separated bars rather than gap-padded zeros.
    """
    if not points:
        return _empty_figure("No events match the current filters.")
    # Pivot to {event: {day: count}}; preserves day ordering by
    # consuming the read-model's `ORDER BY day ASC, event ASC` output.
    by_event: dict[str, dict] = defaultdict(dict)
    days: list = []
    seen_days: set = set()
    for p in points:
        if p.day not in seen_days:
            seen_days.add(p.day)
            days.append(p.day)
        by_event[p.event][p.day] = p.count
    fig = go.Figure()
    event_order = sorted(by_event.keys())
    for event in event_order:
        fig.add_trace(
            go.Bar(
                x=days,
                y=[by_event[event].get(d, 0) for d in days],
                name=event,
                marker_color=theme.color_for(
                    event, event_order, explicit=theme.EVENT_COLORS
                ),
                hovertemplate="%{x}<br>%{y} %{fullData.name}<extra></extra>",
            )
        )
    fig.update_layout(barmode="stack", **theme.base_layout(title=title))
    fig.update_yaxes(title_text="events")
    return fig


def stage_bars(
    rows: Sequence[StageBreakdown],
    *,
    title: str = "Events by stage",
) -> go.Figure:
    """Horizontal bar chart of event counts per stage.

    Horizontal because stage names are long and rotating x-axis
    labels makes them harder to scan. Bars are sorted descending so
    the busiest stage is at the top -- matches the read model's
    `ORDER BY count DESC, stage ASC` shape; passing pre-sorted rows
    through keeps that ordering on the chart.
    """
    if not rows:
        return _empty_figure("No stage data matches the current filters.")
    # Plotly draws the first y-value at the bottom; reverse so the
    # largest count surfaces at the top of the chart.
    ordered = list(rows)[::-1]
    fig = go.Figure(
        go.Bar(
            x=[r.count for r in ordered],
            y=[r.stage for r in ordered],
            orientation="h",
            marker_color=[
                theme.color_for(
                    r.stage,
                    [r.stage for r in ordered],
                    explicit=theme.STAGE_COLORS,
                )
                for r in ordered
            ],
            hovertemplate="%{y}: %{x} events<extra></extra>",
        )
    )
    fig.update_layout(**theme.base_layout(title=title))
    fig.update_xaxes(title_text="events")
    return fig


def review_round_bars(
    rows: Sequence[AgentExitRow],
    *,
    title: str = "Agent runs by review round",
) -> go.Figure:
    """Bar chart of agent runs grouped by `review_round`.

    Rows with `review_round is None` (pre-review work, like the
    initial implementer pass before any reviewer round has run)
    surface as a labeled `0` bucket so the chart still shows them --
    silently dropping them would hide a category that the operator
    expects to see.
    """
    if not rows:
        return _empty_figure(
            "No `agent_exit` rows match the current filters."
        )
    counts: Counter[int] = Counter()
    for r in rows:
        counts[int(r.review_round) if r.review_round is not None else 0] += 1
    ordered_rounds = sorted(counts.keys())
    fig = go.Figure(
        go.Bar(
            x=[str(r) for r in ordered_rounds],
            y=[counts[r] for r in ordered_rounds],
            marker_color=theme.PRIMARY,
            hovertemplate="round %{x}: %{y} runs<extra></extra>",
        )
    )
    fig.update_layout(**theme.base_layout(title=title))
    fig.update_xaxes(title_text="review round", type="category")
    fig.update_yaxes(title_text="agent runs")
    return fig


def repo_bars(
    rows: Sequence[IssueSummaryRow],
    *,
    title: str = "Activity by repo",
) -> go.Figure:
    """Per-repo grouped bars for issue count and total event count.

    `rows` is the issues overview shape -- one entry per
    `(repo, issue)` pair. We aggregate up to per-repo: how many
    distinct issues touched the orchestrator inside the window, and
    how many total events fired for that repo. Both bars share the
    same y-axis (counts), which matches the read-model semantics
    (`event_count` is already an int).
    """
    if not rows:
        return _empty_figure("No issues match the current filters.")
    issues_per_repo: Counter[str] = Counter()
    events_per_repo: Counter[str] = Counter()
    for r in rows:
        issues_per_repo[r.repo] += 1
        events_per_repo[r.repo] += int(r.event_count)
    repos = sorted(
        issues_per_repo.keys(),
        key=lambda repo: (-events_per_repo[repo], repo),
    )
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=repos,
            y=[issues_per_repo[r] for r in repos],
            name="issues",
            marker_color=theme.PRIMARY,
            hovertemplate="%{x}: %{y} issues<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=repos,
            y=[events_per_repo[r] for r in repos],
            name="events",
            marker_color=theme.SECONDARY,
            hovertemplate="%{x}: %{y} events<extra></extra>",
        )
    )
    fig.update_layout(barmode="group", **theme.base_layout(title=title))
    fig.update_yaxes(title_text="count")
    return fig


def cost_coverage(
    rows: Sequence[AgentExitRow],
    *,
    title: str = "Cost source coverage",
) -> go.Figure:
    """Donut chart of `cost_source` distribution.

    Categories with zero rows are elided (Plotly handles that
    automatically). The legend uses the raw `cost_source` strings
    written by `orchestrator.usage` so the labels match what other
    surfaces (logs, JSONL) show. Rows whose `cost_source` is None
    are bucketed under `"unknown"` -- this can happen when an old
    record predates the field; surfacing it as a labeled slice is
    more useful than dropping it.
    """
    if not rows:
        return _empty_figure(
            "No `agent_exit` rows match the current filters."
        )
    counts: Counter[str] = Counter()
    for r in rows:
        counts[r.cost_source or "unknown"] += 1
    labels = list(counts.keys())
    fig = go.Figure(
        go.Pie(
            labels=labels,
            values=[counts[k] for k in labels],
            hole=0.45,
            marker={
                "colors": [
                    theme.color_for(
                        k, labels, explicit=theme.COST_SOURCE_COLORS
                    )
                    for k in labels
                ]
            },
            hovertemplate="%{label}: %{value} (%{percent})<extra></extra>",
        )
    )
    fig.update_layout(**theme.base_layout(title=title))
    return fig


def throughput(
    points: Sequence[TimeSeriesPoint],
    *,
    title: str = "Throughput (events/day)",
) -> go.Figure:
    """Daily totals across all events.

    Sums each day's per-event counts into a single bar so the
    operator sees the orchestrator's per-day activity at a glance
    without the visual noise of stacking. Use ``usage_over_time``
    for the event-by-event view.
    """
    if not points:
        return _empty_figure("No events match the current filters.")
    daily: Counter = Counter()
    for p in points:
        daily[p.day] += int(p.count)
    days = sorted(daily.keys())
    fig = go.Figure(
        go.Bar(
            x=days,
            y=[daily[d] for d in days],
            marker_color=theme.PRIMARY,
            hovertemplate="%{x}: %{y} events<extra></extra>",
        )
    )
    fig.update_layout(**theme.base_layout(title=title))
    fig.update_yaxes(title_text="events")
    return fig


def heatmap_7x24(
    timestamps: Sequence[datetime],
    *,
    title: str = "Activity by weekday × hour",
) -> go.Figure:
    """7×24 heatmap of activity bucketed by weekday and hour.

    `timestamps` is a raw sequence of `datetime`s -- one per event.
    The chart aggregates them locally into a 7-row (Monday-first)
    by 24-column matrix. Naive timestamps are accepted as-is; if
    the caller wants a specific timezone projection (e.g. operator
    local time vs UTC) they should convert before calling.

    An empty input still renders the empty grid (all zeros) so the
    operator can tell "the chart is wired" apart from "no rows"; an
    explicit empty-state annotation labels the situation.
    """
    matrix = [[0] * 24 for _ in range(7)]
    for ts in timestamps:
        matrix[ts.weekday()][ts.hour] += 1
    fig = go.Figure(
        go.Heatmap(
            z=matrix,
            x=[f"{h:02d}" for h in range(24)],
            y=list(_WEEKDAY_LABELS),
            colorscale="Blues",
            hovertemplate=(
                "%{y} %{x}:00 -- %{z} events<extra></extra>"
            ),
        )
    )
    fig.update_layout(**theme.base_layout(title=title))
    fig.update_xaxes(title_text="hour (local)", type="category")
    fig.update_yaxes(title_text="weekday", autorange="reversed")
    if not timestamps:
        fig.add_annotation(
            text="No events match the current filters.",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font={"color": theme.MUTED_TEXT, "size": theme.FONT_SIZE},
        )
    return fig
