# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Plotly figure builders for the redesigned analytics dashboard.

Pure functions: each builder takes already-fetched read-model rows
(or a raw matrix for the 7x24 heatmap) and returns a
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

The chart shapes mirror the redesigned standalone mock (issue #341):

- ``usage_over_time`` -- stacked-area daily token consumption with a
  cost line overlaid on a secondary axis, segmented by either token
  type (Input / Output / Cache) or backend (Claude / Codex).
- ``cost_horizontal_bars`` -- horizontal cost bars used by the
  per-stage, per-review-round, and per-repo panels. Each row carries a
  label, an optional sub-line (e.g. run count), and a single cost
  value rendered at the bar's tip.
- ``hour_weekday_heatmap`` -- weekday-by-hour activity heatmap
  matching the mock's faint-to-saturated accent gradient.
- ``done_per_day_bars`` -- thin per-day bars for the reliability /
  throughput panel.

Reflecting "the same amount of data is enough" from issue #341, no new
read-model dimensions were introduced; ``cost_horizontal_bars`` can
plot per-review-round cost off the ``ReviewRoundBucketRow.total_cost_usd``
column already exposed by ``orchestrator.analytics.read``.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Sequence

import plotly.graph_objects as go

from . import dashboard_theme as theme
from .analytics.read import (
    BackendEfficiencyRow,
    HourlyHeatmapPoint,
    RepoBreakdownRow,
    ReviewRoundBucketRow,
    StageBreakdown,
    ThroughputDayRow,
    TimeSeriesPoint,
)

# Postgres `EXTRACT(DOW FROM ts)` is 0 = Sunday; the standalone mock's
# heatmap renders Sunday-first, so we keep that ordering here too --
# the chart label row drives what the operator reads off the y-axis.
_WEEKDAY_LABELS: tuple[str, ...] = (
    "Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat",
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


def _date_axis(days: Sequence[date]) -> list:
    """Return `days` as date objects; Plotly handles ISO formatting."""
    return list(days)


def usage_over_time(
    points: Sequence[TimeSeriesPoint],
    *,
    backend_rows_by_day: Optional[dict[date, dict[str, float]]] = None,
    mode: str = "type",
    title: str = "Spend & token usage over time",
) -> go.Figure:
    """Hero chart: stacked daily token usage with a cost line overlay.

    `points` is the time-series read-model shape (one row per
    `(day, event, count, cost_usd, input_tokens, output_tokens)`). The
    builder rolls up per-day totals across every event in the window
    so the chart still aggregates correctly when the operator narrows
    the event multiselect to a subset.

    Two stack modes match the standalone mock's segmented control:

    - ``"type"`` (default) stacks daily input / output / (cache_*)
      token volumes. The read model's `input_tokens` and
      `output_tokens` already roll up the per-event totals; cache
      tokens are not exposed at the per-day granularity (only the
      hot-path `agent_exit` rows carry them and they live in `extras`
      on `analytics_events`), so the cache band is omitted rather
      than zeroed in.
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
    if not points and not backend_rows_by_day:
        return _empty_figure("No events match the current filters.")

    daily: dict[date, dict[str, float]] = {}
    for p in points:
        bucket = daily.setdefault(
            p.day,
            {"input": 0.0, "output": 0.0, "cache": 0.0, "cost": 0.0},
        )
        bucket["input"] += float(p.input_tokens or 0)
        bucket["output"] += float(p.output_tokens or 0)
        # Total cache band: cache_read + cache_write -- matching the
        # standalone mock's `r.cr + r.cw` accounting. `cached_tokens`
        # (the cumulative cached count) is deliberately excluded so
        # we do not double-count the same prompt slices.
        bucket["cache"] += float(
            (p.cache_read_tokens or 0) + (p.cache_write_tokens or 0)
        )
        bucket["cost"] += float(p.cost_usd or 0.0)

    if mode == "backend" and backend_rows_by_day:
        for day, by_backend in backend_rows_by_day.items():
            daily.setdefault(
                day,
                {"input": 0.0, "output": 0.0, "cache": 0.0, "cost": 0.0},
            )

    days = sorted(daily.keys())
    if not days:
        return _empty_figure("No events match the current filters.")

    fig = go.Figure()
    if mode == "backend" and backend_rows_by_day:
        backends = sorted({
            b
            for by_backend in backend_rows_by_day.values()
            for b in by_backend
        })
        # Reverse the legend order so the first backend renders at
        # the bottom of the stack (matching how token type is drawn).
        for backend in backends:
            color = theme.color_for(
                backend, backends, explicit=theme.BACKEND_COLORS
            )
            fig.add_trace(
                go.Scatter(
                    x=_date_axis(days),
                    y=[
                        backend_rows_by_day.get(d, {}).get(backend, 0)
                        for d in days
                    ],
                    name=backend,
                    mode="lines",
                    stackgroup="tokens",
                    line={"width": 0.5, "color": color},
                    fillcolor=color,
                    hovertemplate=(
                        "%{x}<br>" + backend +
                        ": %{y:,} tokens<extra></extra>"
                    ),
                )
            )
    else:
        for band, label in (
            ("input", "Input"),
            ("output", "Output"),
            ("cache", "Cache"),
        ):
            color = theme.TOKEN_TYPE_COLORS[label]
            fig.add_trace(
                go.Scatter(
                    x=_date_axis(days),
                    y=[daily[d][band] for d in days],
                    name=label,
                    mode="lines",
                    stackgroup="tokens",
                    line={"width": 0.5, "color": color},
                    fillcolor=color,
                    hovertemplate=(
                        "%{x}<br>" + label +
                        ": %{y:,} tokens<extra></extra>"
                    ),
                )
            )

    fig.add_trace(
        go.Scatter(
            x=_date_axis(days),
            y=[daily[d]["cost"] for d in days],
            name="Cost",
            mode="lines+markers",
            line={"color": theme.INK, "width": 2},
            marker={"size": 5, "color": theme.INK},
            yaxis="y2",
            hovertemplate="%{x}<br>Cost: $%{y:.2f}<extra></extra>",
        )
    )

    layout = theme.base_layout(title=title)
    layout["yaxis"] = {
        **layout.get("yaxis", {}),
        "title": {"text": "tokens"},
    }
    layout["yaxis2"] = {
        "title": {"text": "USD"},
        "overlaying": "y",
        "side": "right",
        "gridcolor": theme.GRID,
        "linecolor": theme.GRID,
        "tickprefix": "$",
        "tickfont": {"color": theme.MUTED_TEXT},
    }
    layout["hovermode"] = "x unified"
    layout["legend"] = {
        **layout.get("legend", {}),
        "orientation": "h",
        "yanchor": "bottom", "y": 1.02,
        "xanchor": "left", "x": 0,
    }
    fig.update_layout(**layout)
    return fig


def cost_horizontal_bars(
    items: Sequence[tuple[str, str, float, str]],
    *,
    title: Optional[str] = None,
    accent: Optional[str] = None,
) -> go.Figure:
    """Horizontal cost bars with per-row sub-label and per-bar value.

    `items` is `(label, sub, cost_usd, color)` per row. `label` is the
    top line (e.g. stage name), `sub` is a small grey line below it
    (e.g. ``"32 runs"``), `cost_usd` is the bar length, and `color`
    is the bar hue. `accent` overrides the default trace color when
    every row carries the same hue (the per-row `color` always wins
    when set).

    The chart is sorted by cost descending so the largest spend sits
    at the top. Each bar is annotated with the dollar amount at its
    tip, matching the standalone mock's labelled bars.
    """
    if not items:
        return _empty_figure("No data matches the current filters.")
    ordered = sorted(items, key=lambda it: -float(it[2] or 0.0))
    labels = [it[0] for it in ordered]
    subs = [it[1] for it in ordered]
    values = [float(it[2] or 0.0) for it in ordered]
    colors = [
        (it[3] if (len(it) > 3 and it[3]) else (accent or theme.ACCENT))
        for it in ordered
    ]
    # Plotly draws the first y-value at the bottom of a horizontal
    # bar chart; reverse so the largest cost sits at the top.
    labels.reverse()
    subs.reverse()
    values.reverse()
    colors.reverse()
    text = [theme.fmt_money(v) for v in values]
    # Compose a Plotly-flavored two-line y-tick where the sub-label
    # renders in muted gray underneath the bold label.
    y_ticks = [
        (
            f"<b>{lbl}</b><br>"
            f"<span style='color:{theme.MUTED_TEXT};font-size:11px'>"
            f"{sub}</span>"
        )
        if sub
        else f"<b>{lbl}</b>"
        for lbl, sub in zip(labels, subs)
    ]
    fig = go.Figure(
        go.Bar(
            x=values,
            y=y_ticks,
            orientation="h",
            marker_color=colors,
            text=text,
            textposition="outside",
            textfont={"color": theme.TEXT, "size": 12,
                      "family": theme.MONO_FONT_FAMILY},
            cliponaxis=False,
            hovertemplate="%{y}: $%{x:,.2f}<extra></extra>",
        )
    )
    layout = theme.base_layout(title=title)
    layout["margin"] = {"l": 160, "r": 64, "t": layout["margin"]["t"], "b": 32}
    fig.update_layout(**layout)
    fig.update_xaxes(
        title_text="USD", tickprefix="$",
        showline=False, zeroline=False,
    )
    fig.update_yaxes(automargin=True, showline=False, ticks="")
    return fig


def cost_by_stage(rows: Sequence[StageBreakdown]) -> go.Figure:
    """Build the per-workflow-stage cost bars.

    Each row carries the stage name as the bar label, the row's
    agent-run count (`StageBreakdown.runs`) as the sub-line, and the
    total cost as the bar length. The sub-line label is "runs" --
    matching the standalone mock, which aggregates per-agent-run
    records, not per-event rows. `StageBreakdown.count` is
    `COUNT(*)` over every `analytics_events` row that carries the
    stage (so it includes `stage_enter` / `stage_evaluation`
    alongside `agent_exit`), which would overstate stage activity
    against the per-run cost; `runs` narrows to the
    `event = 'agent_exit'` subset for the same query.
    """
    if not rows:
        return _empty_figure("No stage data matches the current filters.")
    items = [
        (
            r.stage,
            f"{int(r.runs):,} runs",
            float(r.total_cost_usd or 0.0),
            theme.color_for(r.stage, explicit=theme.STAGE_COLORS),
        )
        for r in rows
    ]
    return cost_horizontal_bars(items)


def cost_by_review_round(rows: Sequence[ReviewRoundBucketRow]) -> go.Figure:
    """Build the per-review-round cost bars.

    `0` is the initial pass; every later bucket is rework. The bucket
    order (`0`, `1`, `2`, `3-5`, `6+`, `unknown`) is fixed by the
    `analytics_agent_runs` view, but Plotly draws horizontal bars
    bottom-up, so `cost_horizontal_bars` re-sorts descending by cost
    -- the operator reads the most expensive round at the top of the
    panel.
    """
    if not rows:
        return _empty_figure(
            "No `agent_exit` rows match the current filters."
        )
    label_map = {
        "0": "Initial",
        "1": "Round 1",
        "2": "Round 2",
        "3-5": "Rounds 3-5",
        "6+": "Rounds 6+",
        "unknown": "Unknown",
    }
    items = [
        (
            label_map.get(r.bucket, r.bucket),
            f"{int(r.runs):,} runs",
            float(r.total_cost_usd or 0.0),
            theme.color_for(r.bucket, explicit=theme.REVIEW_ROUND_COLORS),
        )
        for r in rows
    ]
    return cost_horizontal_bars(items)


def cost_by_repo(rows: Sequence[RepoBreakdownRow]) -> go.Figure:
    """Build the per-repo cost bars.

    Repositories are addressed by their full `owner/name` slug; the
    bar label trims to the short name for legibility while the
    sub-line carries the per-repo agent-run count -- matching the
    standalone mock, which aggregates per-agent-run records, not
    per-event rows. `RepoBreakdownRow.events` (the all-event count)
    would overstate per-repo activity against the per-run cost.
    """
    if not rows:
        return _empty_figure("No repos match the current filters.")
    items = []
    for r in rows:
        short = r.repo.split("/")[-1] if "/" in r.repo else r.repo
        items.append(
            (
                short,
                f"{int(r.agent_exits):,} runs",
                float(r.total_cost_usd or 0.0),
                theme.ACCENT,
            )
        )
    return cost_horizontal_bars(items)


def hour_weekday_heatmap(
    points: Sequence[HourlyHeatmapPoint],
    *,
    title: Optional[str] = None,
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
    `total_tokens`.
    """
    matrix = [[0] * 24 for _ in range(7)]
    for p in points:
        if 0 <= int(p.weekday) < 7 and 0 <= int(p.hour) < 24:
            matrix[int(p.weekday)][int(p.hour)] = int(
                getattr(p, "total_tokens", 0) or 0
            )
    fig = go.Figure(
        go.Heatmap(
            z=matrix,
            x=[f"{h:02d}" for h in range(24)],
            y=list(_WEEKDAY_LABELS),
            colorscale=[
                [0.0, theme.CARD_BG],
                [0.05, "#eae8fb"],
                [1.0, theme.ACCENT],
            ],
            showscale=False,
            xgap=2,
            ygap=2,
            hovertemplate="%{y} %{x}:00 -- %{z:,} tokens<extra></extra>",
        )
    )
    layout = theme.base_layout(title=title)
    layout["margin"] = {"l": 48, "r": 24, "t": layout["margin"]["t"], "b": 32}
    fig.update_layout(**layout)
    fig.update_xaxes(title_text="hour (UTC)", type="category", showgrid=False)
    fig.update_yaxes(title_text="", autorange="reversed", showgrid=False)
    if not points:
        fig.add_annotation(
            text="No events match the current filters.",
            x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
            font={"color": theme.MUTED_TEXT, "size": theme.FONT_SIZE},
        )
    return fig


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
    days_index = {r.day: int(r.resolved or 0) for r in rows}
    if window_start is not None and window_end is not None:
        days: list[date] = []
        d = window_start
        # Walk inclusive end day by day; using `timedelta` keeps the
        # rollover safe across month / year boundaries.
        while d <= window_end:
            days.append(d)
            d = d + timedelta(days=1)
    else:
        days = sorted(days_index)
    if not days:
        return _empty_figure("No resolved issues in the current window.")
    resolved = [days_index.get(d, 0) for d in days]
    fig = go.Figure(
        go.Bar(
            x=days,
            y=resolved,
            marker_color=theme.SUCCESS,
            hovertemplate="%{x}: %{y} resolved<extra></extra>",
        )
    )
    layout = theme.base_layout(title=title)
    layout["margin"] = {"l": 40, "r": 16, "t": layout["margin"]["t"], "b": 32}
    layout["yaxis"] = {
        **layout.get("yaxis", {}),
        "title": {"text": "resolved"},
    }
    fig.update_layout(**layout)
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
    _ = rows
    return {}
