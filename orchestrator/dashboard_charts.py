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
  per-stage and per-repo panels. Each row carries a label, an optional
  sub-line (e.g. run count), and a single cost value rendered at the
  bar's tip.
- ``cost_by_review_round`` -- grouped horizontal bars per review round,
  split into development and review cost from ``ReviewRoundBucketRow``.
- ``hour_weekday_heatmap`` -- weekday-by-hour activity heatmap
  matching the mock's faint-to-saturated accent gradient.
- ``done_per_day_bars`` -- thin per-day bars for the reliability /
  throughput panel.

Reflecting "the same amount of data is enough" from issue #341, the
dashboard still reads the same agent-exit row set; ``cost_by_review_round``
now separates developer and reviewer cost off the role-split columns
exposed by ``orchestrator.analytics.read``.
"""
from __future__ import annotations

import math
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


def _empty_figure(message: str, *, height: int) -> go.Figure:
    """Return a placeholder figure with a centered annotation.

    Plotly raises no error on an empty data series, but the default
    "blank canvas" is a confusing empty-state. Every builder routes
    its no-data branch through here so the user sees a single
    consistent "nothing matches" label across charts. `height` mirrors
    the builder's pinned non-empty height so empty cards do not snap
    to Plotly's 450px default and dwarf surrounding cards.
    """
    fig = go.Figure()
    layout = theme.base_layout()
    layout["height"] = height
    fig.update_layout(**layout)
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


def _nice_axis_max(value: float, steps: int) -> float:
    """Smallest "nice" axis maximum >= `value`, divisible into `steps`
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
    if value <= 0 or steps <= 0:
        return float(max(steps, 1))
    rough = value / steps
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


def usage_over_time(
    points: Sequence[TimeSeriesPoint],
    *,
    backend_rows_by_day: Optional[dict[date, dict[str, float]]] = None,
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
    if not points and not backend_rows_by_day:
        return _empty_figure(
            "No events match the current filters.", height=330,
        )

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
        return _empty_figure(
            "No events match the current filters.", height=330,
        )

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
    # Align the two y-axes on a shared set of horizontal gridlines.
    # Both axes start at zero and split into the same number of equal
    # steps, so each tokens gridline and its USD counterpart sit on the
    # same pixel row. Without this Plotly picks independent "nice"
    # ranges (e.g. 6 token lines from 0 to 1B vs 5 USD lines to $1000)
    # whose gridlines visually drift apart. Only the left (tokens) axis
    # draws the gridlines; the right (USD) axis ticks land on them.
    if mode == "backend" and backend_rows_by_day:
        stack_totals = [
            sum(backend_rows_by_day.get(d, {}).values()) for d in days
        ]
    else:
        stack_totals = [
            daily[d]["input"] + daily[d]["output"] + daily[d]["cache"]
            for d in days
        ]
    token_max = max(stack_totals, default=0.0)
    cost_max = max((daily[d]["cost"] for d in days), default=0.0)
    grid_steps = 5
    token_top = _nice_axis_max(token_max, grid_steps)
    cost_top = _nice_axis_max(cost_max, grid_steps)
    layout["yaxis"] = {
        **layout.get("yaxis", {}),
        "title": {"text": "tokens"},
        "range": [0, token_top],
        "dtick": token_top / grid_steps,
        "rangemode": "tozero",
        "showgrid": True,
    }
    layout["yaxis2"] = {
        "title": {"text": "USD"},
        "overlaying": "y",
        "side": "right",
        "range": [0, cost_top],
        "dtick": cost_top / grid_steps,
        "rangemode": "tozero",
        "gridcolor": theme.GRID,
        "linecolor": theme.GRID,
        "showgrid": False,
        "tickprefix": "$",
        "tickfont": {"color": theme.MUTED_TEXT},
    }
    # The card header already prints the chart title, so `title` is
    # passed as None from the dashboard; keep enough top margin for the
    # horizontal legend that floats just above the plot area.
    layout["margin"] = {**layout.get("margin", {}), "t": 28}
    layout["hovermode"] = "x unified"
    layout["legend"] = {
        **layout.get("legend", {}),
        "orientation": "h",
        "yanchor": "bottom", "y": 1.02,
        "xanchor": "left", "x": 0,
    }
    # Hero chart: ~330px matches the standalone mock. Plotly's
    # default 450px leaves the chart looking over-tall against the
    # surrounding KPI strip and stage-cost cards.
    layout["height"] = 330
    fig.update_layout(**layout)
    return fig


def cost_horizontal_bars(
    items: Sequence[tuple[str, str, float, str]],
    *,
    title: Optional[str] = None,
    accent: Optional[str] = None,
    preserve_order: bool = False,
    height: Optional[int] = None,
) -> go.Figure:
    """Horizontal cost bars with per-row sub-label and per-bar value.

    `items` is `(label, sub, cost_usd, color)` per row. `label` is the
    top line (e.g. stage name), `sub` is a small grey line below it
    (e.g. ``"32 runs"``), `cost_usd` is the bar length, and `color`
    is the bar hue. `accent` overrides the default trace color when
    every row carries the same hue (the per-row `color` always wins
    when set).

    By default the chart is sorted by cost descending so the largest
    spend sits at the top. Pass `preserve_order=True` to keep the
    caller's order instead (e.g. review rounds, which read best in
    logical Initial -> 1 -> ... -> 6+ -> Unknown order rather than by
    cost). `height` overrides the auto-computed panel height so two
    paired panels can be pinned to the same height.
    """
    if not items:
        # Match the single-row non-empty case (`40 * 1 + 80`) so an
        # empty card sits at the same minimum height instead of
        # snapping to Plotly's 450px default.
        return _empty_figure(
            "No data matches the current filters.",
            height=height or 120,
        )
    ordered = (
        list(items)
        if preserve_order
        else sorted(items, key=lambda it: -float(it[2] or 0.0))
    )
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
    # Size the panel to the bar count: ~40px per row plus a fixed
    # top / bottom margin. Plotly's 450px default makes a 3-row
    # panel float in an empty box; a 6-row panel still fits inside
    # the same hero-chart height. An explicit `height` (e.g. to match
    # a paired panel) overrides the per-row computation.
    n_rows = max(len(values), 1)
    layout["height"] = height if height is not None else 40 * n_rows + 80
    fig.update_layout(**layout)
    fig.update_xaxes(
        title_text="USD", tickprefix="$",
        showline=False, zeroline=False,
    )
    fig.update_yaxes(automargin=True, showline=False, ticks="")
    return fig


def cost_by_stage(
    rows: Sequence[StageBreakdown], *, height: Optional[int] = None
) -> go.Figure:
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
    `event = 'agent_exit'` subset for the same query. `height` is
    forwarded so the panel can be pinned to a paired panel's height.
    """
    if not rows:
        return _empty_figure(
            "No stage data matches the current filters.",
            height=height or 120,
        )
    items = [
        (
            r.stage,
            f"{int(r.runs):,} runs",
            float(r.total_cost_usd or 0.0),
            theme.color_for(r.stage, explicit=theme.STAGE_COLORS),
        )
        for r in rows
    ]
    return cost_horizontal_bars(items, height=height)


def cost_by_review_round(
    rows: Sequence[ReviewRoundBucketRow], *, height: Optional[int] = None
) -> go.Figure:
    """Build grouped development/review cost bars per review round.

    `0` is the initial development/review cycle; every later round is
    rework. Buckets are rendered in logical order -- Initial -> Round 1
    -> ... -> Round 5 -> Rounds 6+ -> No review round, top to bottom --
    rather than sorted by cost, so the operator reads the rework
    progression in sequence. Each row renders two bars: development
    cost (`agent_role=developer`) and review cost (`agent_role=reviewer`).
    `get_review_round_breakdown` keeps rounds 3, 4 and 5 separate
    (only 6+ is grouped). `height` is forwarded so the panel can be
    pinned to the workflow-stage panel's height.
    """
    if not rows:
        return _empty_figure(
            "No `agent_exit` rows match the current filters.",
            height=height or 120,
        )
    label_map = {
        "0": "Initial",
        "1": "Round 1",
        "2": "Round 2",
        "3": "Round 3",
        "4": "Round 4",
        "5": "Round 5",
        "6+": "Rounds 6+",
        "unknown": "No review round",
    }
    # Logical sequence before Plotly's horizontal-bar reversal:
    # Initial -> Round 1 -> ... -> No review round.
    order = ["0", "1", "2", "3", "4", "5", "6+", "unknown"]
    by_bucket = {r.bucket: r for r in rows}
    ordered_rows = [by_bucket[b] for b in order if b in by_bucket]
    if not ordered_rows:
        return _empty_figure(
            "No development or review runs match the current filters.",
            height=height or 120,
        )
    labels = [label_map.get(r.bucket, r.bucket) for r in ordered_rows]
    subs = [
        (
            f"{int(r.developer_runs or 0):,} dev / "
            f"{int(r.reviewer_runs or 0):,} review runs"
        )
        for r in ordered_rows
    ]
    dev_values = [float(r.developer_cost_usd or 0.0) for r in ordered_rows]
    review_values = [float(r.reviewer_cost_usd or 0.0) for r in ordered_rows]

    # Plotly draws the first y-value at the bottom of a horizontal
    # grouped bar chart, so reverse to keep Initial at the top.
    labels.reverse()
    subs.reverse()
    dev_values.reverse()
    review_values.reverse()
    y_ticks = [
        (
            f"<b>{lbl}</b><br>"
            f"<span style='color:{theme.MUTED_TEXT};font-size:11px'>"
            f"{sub}</span>"
        )
        for lbl, sub in zip(labels, subs)
    ]
    fig = go.Figure()
    # For horizontal grouped bars, Plotly lays traces bottom-to-top
    # within each y bucket. Add Review first so the visible pair reads
    # Development, then Review from top to bottom.
    for name, values, role in (
        ("Review", review_values, "reviewer"),
        ("Development", dev_values, "developer"),
    ):
        fig.add_trace(
            go.Bar(
                x=values,
                y=y_ticks,
                name=name,
                orientation="h",
                marker_color=theme.AGENT_ROLE_COLORS[role],
                text=[theme.fmt_money(v) for v in values],
                textposition="outside",
                textfont={
                    "color": theme.TEXT,
                    "size": 12,
                    "family": theme.MONO_FONT_FAMILY,
                },
                cliponaxis=False,
                hovertemplate=(
                    "%{y}<br>" + name + ": $%{x:,.2f}<extra></extra>"
                ),
            )
        )
    layout = theme.base_layout()
    layout["barmode"] = "group"
    layout["margin"] = {"l": 160, "r": 64, "t": layout["margin"]["t"], "b": 32}
    layout["height"] = height if height is not None else 44 * len(y_ticks) + 90
    layout["legend"] = {
        "orientation": "h",
        "x": 0,
        "y": 1.12,
        "xanchor": "left",
        "yanchor": "bottom",
        "traceorder": "reversed",
    }
    fig.update_layout(**layout)
    fig.update_xaxes(
        title_text="USD", tickprefix="$",
        showline=False, zeroline=False,
    )
    fig.update_yaxes(automargin=True, showline=False, ticks="")
    return fig


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
        return _empty_figure(
            "No repos match the current filters.", height=120,
        )
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
    # 7 rows x 24 columns: ~240px keeps the cells close to the
    # mock's compact squares instead of stretching them into tall
    # rectangles at the default 450px.
    layout["height"] = 240
    # Paint the plot background the border colour so the `xgap`/`ygap`
    # between cells reads as a weekday x hour grid. Zero-volume cells
    # are white (colorscale[0] == CARD_BG), so without a contrasting
    # backdrop the gaps vanish and the sparse right-hand hours look
    # like missing data rather than empty cells.
    layout["plot_bgcolor"] = theme.BORDER
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
        return _empty_figure(
            "No resolved issues in the current window.", height=150,
        )
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
    # The throughput strip sits in the narrow reliability column;
    # at the 450px Plotly default it would dwarf the tiles above.
    # 150px matches the standalone mock's thin per-day strip.
    layout["height"] = 150
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
