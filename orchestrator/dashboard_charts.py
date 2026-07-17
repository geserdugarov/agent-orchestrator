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
  per-repo panel. Each row carries a label, an optional sub-line
  (e.g. run count), and a single cost value rendered at the bar's
  tip.
- ``cost_by_stage`` -- horizontal cost bars per workflow stage with
  each bar stacked into no-cache + cache cost from
  ``StageBreakdown.no_cache_cost_usd`` / ``cache_cost_usd`` so the
  operator can see how much per-stage spend was prorated through
  cached tokens vs charged against fresh input + output tokens.
- ``cost_by_review_round`` -- grouped horizontal bars per review round,
  split into development and review cost from ``ReviewRoundBucketRow``;
  each role's bar is further stacked into no-cache + cache cost so the
  operator can see how much per-round spend was prorated through cached
  tokens vs charged against fresh input + output tokens.
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
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Sequence

import plotly.graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator.analytics.read import (
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

# The activity heatmap is a fixed 7-weekday-row x 24-hour-column grid. The
# row count follows `_WEEKDAY_LABELS` so the matrix and the y-axis labels
# cannot drift apart; the column span is the hours in a day.
_HOURS_PER_DAY = 24
_USAGE_GRID_STEPS = 5
_HORIZONTAL_BAR_ROW_HEIGHT = 40
_HORIZONTAL_BAR_EXTRA_HEIGHT = 80
_REVIEW_BAR_ROW_HEIGHT = 44
_REVIEW_BAR_EXTRA_HEIGHT = 90
_HORIZONTAL_BAR_MARGIN = {"l": 160, "r": 64, "b": 32}

_REVIEW_ROUND_LABELS = {
    "0": "Initial",
    "1": "Round 1",
    "2": "Round 2",
    "3": "Round 3",
    "4": "Round 4",
    "5": "Round 5",
    "6+": "Rounds 6+",
    "unknown": "No review round",
}
_REVIEW_ROUND_ORDER = ("0", "1", "2", "3", "4", "5", "6+", "unknown")


# Token-usage band identifiers, shared by the daily accumulators and the
# stacked-area series builders.
_INPUT = "input"
_OUTPUT = "output"
_CACHE = "cache"
_COST = "cost"
# Default rendered chart height in px when a caller does not override it.
_DEFAULT_CHART_HEIGHT = 120
# Lightening ratio applied to a series color for its cache sub-band.
_CACHE_LIGHTEN = 0.45
# Base for parsing a "#rrggbb" hex color component.
_HEX_BASE = 16


def _empty_token_bucket() -> dict[str, float]:
    """A fresh per-day accumulator with every token/cost band zeroed."""
    return {_INPUT: 0.0, _OUTPUT: 0.0, _CACHE: 0.0, _COST: 0.0}


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


def _money_text(amounts: Sequence[float]) -> list[str]:
    return [theme.fmt_money(amount) for amount in amounts]


def _monospace_textfont() -> dict[str, object]:
    return {
        "color": theme.TEXT,
        "size": 12,
        "family": theme.MONO_FONT_FAMILY,
    }


def _two_line_y_ticks(
    labels: Sequence[str], subs: Sequence[str]
) -> list[str]:
    ticks: list[str] = []
    for label, sub in zip(labels, subs):
        label_html = f"<b>{label}</b>"
        if sub:
            ticks.append(
                f"{label_html}<br>"
                f"<span style='color:{theme.MUTED_TEXT};font-size:11px'>"
                f"{sub}</span>"
            )
        else:
            ticks.append(label_html)
    return ticks


def _reverse_lists(*sequences: Sequence) -> tuple[list, ...]:
    return tuple(list(reversed(sequence)) for sequence in sequences)


def _horizontal_panel_height(
    row_count: int,
    *,
    height: Optional[int],
    row_height: int = _HORIZONTAL_BAR_ROW_HEIGHT,
    extra_height: int = _HORIZONTAL_BAR_EXTRA_HEIGHT,
) -> int:
    if height is not None:
        return height
    return row_height * max(row_count, 1) + extra_height


def _horizontal_legend(*, traceorder: Optional[str] = None) -> dict[str, object]:
    legend: dict[str, object] = {
        "orientation": "h",
        "x": 0,
        "y": 1.12,
        "xanchor": "left",
        "yanchor": "bottom",
    }
    if traceorder is not None:
        legend["traceorder"] = traceorder
    return legend


@dataclass(frozen=True)
class _HorizontalCostLayout:
    row_count: int
    height: Optional[int] = None
    title: Optional[str] = None
    barmode: Optional[str] = None
    legend: Optional[dict[str, object]] = None
    row_height: int = _HORIZONTAL_BAR_ROW_HEIGHT
    extra_height: int = _HORIZONTAL_BAR_EXTRA_HEIGHT


def _apply_horizontal_cost_layout(
    fig: go.Figure,
    options: _HorizontalCostLayout,
) -> None:
    layout = theme.base_layout(title=options.title)
    if options.barmode is not None:
        layout["barmode"] = options.barmode
    if options.legend is not None:
        layout["legend"] = options.legend
    layout["margin"] = {
        **_HORIZONTAL_BAR_MARGIN,
        "t": layout["margin"]["t"],
    }
    layout["height"] = _horizontal_panel_height(
        options.row_count,
        height=options.height,
        row_height=options.row_height,
        extra_height=options.extra_height,
    )
    fig.update_layout(**layout)
    fig.update_xaxes(
        title_text="USD", tickprefix="$",
        showline=False, zeroline=False,
    )
    fig.update_yaxes(automargin=True, showline=False, ticks="")


@dataclass(frozen=True)
class _CostBarTrace:
    name: str
    amounts: Sequence[float]
    y_ticks: Sequence[str]
    color: object
    hover_label: str
    offsetgroup: Optional[str] = None
    totals: Optional[Sequence[float]] = None


def _cost_bar_trace(
    options: _CostBarTrace,
) -> go.Bar:
    trace_kwargs = {
        "x": list(options.amounts),
        "y": list(options.y_ticks),
        "name": options.name,
        "orientation": "h",
        "marker_color": options.color,
        "cliponaxis": False,
        "hovertemplate": (
            f"%{{y}}<br>{options.hover_label}: $%{{x:,.2f}}<extra></extra>"
        ),
    }
    if options.offsetgroup is not None:
        trace_kwargs["offsetgroup"] = options.offsetgroup
    if options.totals is not None:
        trace_kwargs["text"] = _money_text(options.totals)
        trace_kwargs["textposition"] = "outside"
        trace_kwargs["textfont"] = _monospace_textfont()
    return go.Bar(**trace_kwargs)


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
) -> dict[date, dict[str, float]]:
    daily: dict[date, dict[str, float]] = {}
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
    daily: dict[date, dict[str, float]],
    backend_rows_by_day: dict[date, dict[str, float]],
) -> None:
    for day in backend_rows_by_day:
        daily.setdefault(
            day,
            _empty_token_bucket(),
        )


def _backend_names(
    backend_rows_by_day: dict[date, dict[str, float]],
) -> list[str]:
    return sorted({
        backend
        for by_backend in backend_rows_by_day.values()
        for backend in by_backend
    })


def _usage_stack_totals(
    days: Sequence[date],
    daily: dict[date, dict[str, float]],
    *,
    backend_rows_by_day: Optional[dict[date, dict[str, float]]],
    mode: str,
) -> list[float]:
    if mode == "backend" and backend_rows_by_day:
        return [sum(backend_rows_by_day.get(day, {}).values()) for day in days]
    return [_daily_token_total(daily[day]) for day in days]


def _daily_token_total(bucket: dict[str, float]) -> float:
    return sum(bucket[token_type] for token_type in (_INPUT, _OUTPUT, _CACHE))


@dataclass(frozen=True)
class _UsageChartData:
    daily: dict[date, dict[str, float]]
    days: Sequence[date]


@dataclass(frozen=True)
class _UsageAxisRanges:
    token_top: float
    cost_top: float


def _prepare_usage_data(
    points: Sequence[TimeSeriesPoint],
    backend_rows_by_day: Optional[dict[date, dict[str, float]]],
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
    backend_rows_by_day: dict[date, dict[str, float]],
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
    backend_rows_by_day: Optional[dict[date, dict[str, float]]],
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
    backend_rows_by_day: Optional[dict[date, dict[str, float]]],
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
    backend_rows_by_day: Optional[dict[date, dict[str, float]]],
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


@dataclass(frozen=True)
class _HorizontalBars:
    labels: Sequence[str]
    subs: Sequence[str]
    costs: Sequence[float]
    colors: Sequence[str]


def _reverse_horizontal_bars(bars: _HorizontalBars) -> _HorizontalBars:
    reversed_values = _reverse_lists(
        bars.labels, bars.subs, bars.costs, bars.colors,
    )
    return _HorizontalBars(*reversed_values)


def _horizontal_bars_data(
    rows: Sequence[tuple[str, str, float, str]],
    accent: Optional[str],
    preserve_order: bool,
) -> _HorizontalBars:
    ordered = list(rows)
    if not preserve_order:
        ordered.sort(key=_cost_item_sort_key)
    return _reverse_horizontal_bars(_HorizontalBars(
        labels=[row[0] for row in ordered],
        subs=[row[1] for row in ordered],
        costs=[float(row[2] or 0) for row in ordered],
        colors=[row[3] or accent or theme.ACCENT for row in ordered],
    ))


def _cost_item_sort_key(row: tuple[str, str, float, str]) -> float:
    cost = float(row[2] or 0)
    return -cost


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
            height=height or _DEFAULT_CHART_HEIGHT,
        )
    bars = _horizontal_bars_data(items, accent, preserve_order)
    fig = go.Figure(
        go.Bar(
            x=bars.costs,
            y=_two_line_y_ticks(bars.labels, bars.subs),
            orientation="h",
            marker_color=bars.colors,
            text=_money_text(bars.costs),
            textposition="outside",
            textfont=_monospace_textfont(),
            cliponaxis=False,
            hovertemplate="%{y}: $%{x:,.2f}<extra></extra>",
        )
    )
    # Size the panel to the bar count: ~40px per row plus a fixed
    # top / bottom margin. Plotly's 450px default makes a 3-row
    # panel float in an empty box; a 6-row panel still fits inside
    # the same hero-chart height. An explicit `height` (e.g. to match
    # a paired panel) overrides the per-row computation.
    _apply_horizontal_cost_layout(
        fig,
        _HorizontalCostLayout(
            title=title,
            row_count=len(bars.costs),
            height=height,
        ),
    )
    return fig


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
    ordered = sorted(
        rows, key=lambda row: -float(row.total_cost_usd or 0),
    )
    colors = [
        theme.color_for(row.stage, explicit=theme.STAGE_COLORS)
        for row in ordered
    ]
    return _reverse_stage_cost_bars(_StageCostBars(
        labels=[row.stage for row in ordered],
        subs=[f"{int(row.runs or 0):,} runs" for row in ordered],
        no_cache=[_stage_no_cache_cost(row) for row in ordered],
        cache=[float(row.cache_cost_usd or 0) for row in ordered],
        totals=[float(row.total_cost_usd or 0) for row in ordered],
        colors=colors,
        cache_colors=[_lighten_hex(color, _CACHE_LIGHTEN) for color in colors],
    ))


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
    `event = 'agent_exit'` subset for the same query.

    Each stage's bar is stacked into no-cache + cache cost (each
    rollup row's cost prorated by the share of its tokens that were
    cached / cache-read / cache-write vs the remaining input + output
    tokens). The cache segment uses a translucent shade of the stage
    color so the pair stays visibly tied to the stage. `height` is
    forwarded so the panel can be pinned to a paired panel's height.
    """
    if not rows:
        return _empty_figure(
            "No stage data matches the current filters.",
            height=height or _DEFAULT_CHART_HEIGHT,
        )
    bars = _stage_cost_bars(rows)
    y_ticks = _two_line_y_ticks(bars.labels, bars.subs)
    fig = go.Figure()
    # No-cache trace is added first so it reads as the base segment
    # and cache stacks outward -- matching the issue's "with and
    # without cache parts" framing. Only the outer (cache) trace
    # carries the per-stage total text so the dollar label lands once
    # per bar instead of duplicating on each segment.
    fig.add_trace(
        _cost_bar_trace(
            _CostBarTrace(
                name="No cache",
                amounts=bars.no_cache,
                y_ticks=y_ticks,
                color=bars.colors,
                hover_label="No cache",
            ),
        )
    )
    fig.add_trace(
        _cost_bar_trace(
            _CostBarTrace(
                name="Cache",
                amounts=bars.cache,
                y_ticks=y_ticks,
                color=bars.cache_colors,
                hover_label="Cache",
                totals=bars.totals,
            ),
        )
    )
    _apply_horizontal_cost_layout(
        fig,
        _HorizontalCostLayout(
            row_count=len(y_ticks),
            height=height,
            barmode="stack",
            legend=_horizontal_legend(),
        ),
    )
    return fig


def _lighten_hex(hex_color: str, alpha: float) -> str:
    """Return an `rgba(...)` string of `hex_color` with `alpha`.

    Used to derive the cache-portion shade from the role's base color
    so the no-cache and cache segments stay visibly paired without
    introducing a second palette. Caller passes a `#rrggbb` value;
    short forms / named colors are out of scope -- the chart palette
    only emits 6-hex strings.
    """
    hex_digits = hex_color.lstrip("#")
    red = int(hex_digits[:2], _HEX_BASE)
    green = int(hex_digits[2:4], _HEX_BASE)
    blue = int(hex_digits[4:6], _HEX_BASE)
    return f"rgba({red},{green},{blue},{alpha:.2f})"


@dataclass(frozen=True)
class _ReviewCostBars:
    labels: Sequence[str]
    subs: Sequence[str]
    developer_no_cache: Sequence[float]
    developer_cache: Sequence[float]
    reviewer_no_cache: Sequence[float]
    reviewer_cache: Sequence[float]
    developer_totals: Sequence[float]
    reviewer_totals: Sequence[float]


def _developer_cost_total(row: ReviewRoundBucketRow) -> float:
    no_cache = float(row.developer_no_cache_cost_usd or 0)
    cache = float(row.developer_cache_cost_usd or 0)
    return no_cache + cache


def _reviewer_cost_total(row: ReviewRoundBucketRow) -> float:
    no_cache = float(row.reviewer_no_cache_cost_usd or 0)
    cache = float(row.reviewer_cache_cost_usd or 0)
    return no_cache + cache


def _reverse_review_cost_bars(bars: _ReviewCostBars) -> _ReviewCostBars:
    reversed_values = _reverse_lists(
        bars.labels,
        bars.subs,
        bars.developer_no_cache,
        bars.developer_cache,
        bars.reviewer_no_cache,
        bars.reviewer_cache,
        bars.developer_totals,
        bars.reviewer_totals,
    )
    return _ReviewCostBars(*reversed_values)


def _review_cost_bars(
    rows: Sequence[ReviewRoundBucketRow],
) -> Optional[_ReviewCostBars]:
    by_bucket = {row.bucket: row for row in rows}
    ordered = [
        by_bucket[bucket]
        for bucket in _REVIEW_ROUND_ORDER
        if bucket in by_bucket
    ]
    if not ordered:
        return None
    return _reverse_review_cost_bars(_ReviewCostBars(
        labels=[_REVIEW_ROUND_LABELS.get(row.bucket, row.bucket) for row in ordered],
        subs=[
            f"{int(row.developer_runs or 0):,} dev / "
            f"{int(row.reviewer_runs or 0):,} review runs"
            for row in ordered
        ],
        developer_no_cache=[
            float(row.developer_no_cache_cost_usd or 0) for row in ordered
        ],
        developer_cache=[
            float(row.developer_cache_cost_usd or 0) for row in ordered
        ],
        reviewer_no_cache=[
            float(row.reviewer_no_cache_cost_usd or 0) for row in ordered
        ],
        reviewer_cache=[
            float(row.reviewer_cache_cost_usd or 0) for row in ordered
        ],
        developer_totals=[_developer_cost_total(row) for row in ordered],
        reviewer_totals=[_reviewer_cost_total(row) for row in ordered],
    ))


def _review_cost_traces(
    bars: _ReviewCostBars,
    y_ticks: Sequence[str],
) -> tuple[_CostBarTrace, ...]:
    developer_color = theme.AGENT_ROLE_COLORS["developer"]
    reviewer_color = theme.AGENT_ROLE_COLORS["reviewer"]
    developer_cache_color = _lighten_hex(developer_color, _CACHE_LIGHTEN)
    reviewer_cache_color = _lighten_hex(reviewer_color, _CACHE_LIGHTEN)
    return (
        _CostBarTrace(
            name="Review (no cache)",
            amounts=bars.reviewer_no_cache,
            y_ticks=y_ticks,
            color=reviewer_color,
            offsetgroup="reviewer",
            hover_label="Review (no cache)",
        ),
        _CostBarTrace(
            name="Review (cache)",
            amounts=bars.reviewer_cache,
            y_ticks=y_ticks,
            color=reviewer_cache_color,
            offsetgroup="reviewer",
            totals=bars.reviewer_totals,
            hover_label="Review (cache)",
        ),
        _CostBarTrace(
            name="Development (no cache)",
            amounts=bars.developer_no_cache,
            y_ticks=y_ticks,
            color=developer_color,
            offsetgroup="developer",
            hover_label="Development (no cache)",
        ),
        _CostBarTrace(
            name="Development (cache)",
            amounts=bars.developer_cache,
            y_ticks=y_ticks,
            color=developer_cache_color,
            offsetgroup="developer",
            totals=bars.developer_totals,
            hover_label="Development (cache)",
        ),
    )


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
    Each role's bar is further stacked into no-cache + cache cost
    (each run's cost prorated by the share of its tokens that were
    cached / cache-read / cache-write vs the remaining input + output
    tokens). The cache segment uses a translucent shade of the role
    color so the pair stays visibly tied to the role.
    `get_review_round_breakdown` keeps rounds 3, 4 and 5 separate
    (only 6+ is grouped). `height` is forwarded so the panel can be
    pinned to the workflow-stage panel's height.
    """
    if not rows:
        return _empty_figure(
            "No `agent_exit` rows match the current filters.",
            height=height or _DEFAULT_CHART_HEIGHT,
        )
    bars = _review_cost_bars(rows)
    if bars is None:
        return _empty_figure(
            "No development or review runs match the current filters.",
            height=height or _DEFAULT_CHART_HEIGHT,
        )
    y_ticks = _two_line_y_ticks(bars.labels, bars.subs)
    fig = go.Figure()
    # Horizontal grouped+stacked layout: `offsetgroup` controls the
    # side-by-side offset (one for development, one for review); two
    # traces sharing an `offsetgroup` stack at that offset under
    # `barmode="relative"`. Plotly lays traces bottom-to-top within
    # each y bucket, so adding the Review pair first keeps the visible
    # role order Development above Review per round. Within each
    # offset, the no-cache trace is added before the cache trace so
    # no-cache reads as the base segment and cache stacks outward --
    # matching the issue's "no-cache + cache in stacked form" framing.
    for trace in _review_cost_traces(bars, y_ticks):
        # Only the outer (cache) trace carries the per-role total
        # text so the dollar label still lands once per role bar
        # instead of duplicating on each segment.
        fig.add_trace(
            _cost_bar_trace(trace)
        )
    # `relative` honors `offsetgroup` so same-offset traces stack and
    # different-offset traces sit side-by-side. With all-positive
    # values this acts identically to `stack` per offset.
    _apply_horizontal_cost_layout(
        fig,
        _HorizontalCostLayout(
            row_count=len(y_ticks),
            height=height,
            barmode="relative",
            legend=_horizontal_legend(traceorder="reversed"),
            row_height=_REVIEW_BAR_ROW_HEIGHT,
            extra_height=_REVIEW_BAR_EXTRA_HEIGHT,
        ),
    )
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
            "No repos match the current filters.", height=_DEFAULT_CHART_HEIGHT,
        )
    bar_rows = []
    for row in rows:
        bar_rows.append(
            (
                _repo_short_name(row.repo),
                f"{int(row.agent_exits):,} runs",
                float(row.total_cost_usd or 0),
                theme.ACCENT,
            )
        )
    return cost_horizontal_bars(bar_rows)


def _repo_short_name(repo: str) -> str:
    if "/" not in repo:
        return repo
    return repo.rsplit("/", maxsplit=1)[-1]


def _valid_heatmap_point(point: HourlyHeatmapPoint, weekdays: int) -> bool:
    valid_weekday = 0 <= int(point.weekday) < weekdays
    valid_hour = 0 <= int(point.hour) < _HOURS_PER_DAY
    return valid_weekday and valid_hour


def _heatmap_matrix(
    points: Sequence[HourlyHeatmapPoint],
) -> list[list[int]]:
    weekdays = len(_WEEKDAY_LABELS)
    matrix = [[0] * _HOURS_PER_DAY for _ in range(weekdays)]
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
            x=[f"{hour:02d}" for hour in range(_HOURS_PER_DAY)],
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
            "No resolved issues in the current window.", height=150,
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
    return {}
