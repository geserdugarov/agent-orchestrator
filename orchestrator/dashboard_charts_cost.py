# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Cost-bar Plotly figure builders for the analytics dashboard.

Home of the horizontal cost-bar chart family: the per-repo / per-stage /
per-review-round cost bars (`cost_horizontal_bars`, `cost_by_stage`,
`cost_by_review_round`, `cost_by_repo`) plus the private layout / trace /
row-shaping helpers and the review-bar layout constants they use.

`orchestrator.dashboard_charts` re-exports the four public builders under
their original names so `dashboard_charts.cost_*` keeps resolving for the
widget pipeline. The shared low-level chart primitives (`_empty_figure`, the
money / mono-textfont / two-line-tick helpers, the panel-height / legend
helpers, and the horizontal-bar row/extra-height constants) live in
`orchestrator.dashboard_charts_base`; this module imports them from that base,
not from `dashboard_charts`, so the dependency runs one way and a direct
import of this module is cycle-free.

Like `dashboard_charts`, plotly is imported at module load here: this
module is only reachable from the lazy `import dashboard_charts` inside
`orchestrator.dashboard.main` (which pulls it in via the compatibility
re-export), so the polling tick never imports it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator.analytics.read import (
    RepoBreakdownRow,
    ReviewRoundBucketRow,
    StageBreakdown,
)
from orchestrator.dashboard_charts_base import (
    _empty_figure,
    _horizontal_legend,
    _horizontal_panel_height,
    _money_text,
    _monospace_textfont,
    _reverse_lists,
    _two_line_y_ticks,
)
from orchestrator.dashboard_charts_base import (
    _HORIZONTAL_BAR_EXTRA_HEIGHT,
    _HORIZONTAL_BAR_ROW_HEIGHT,
)

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

# Default rendered chart height in px when a caller does not override it.
_DEFAULT_CHART_HEIGHT = 120
# Lightening ratio applied to a series color for its cache sub-band.
_CACHE_LIGHTEN = 0.45
# Base for parsing a "#rrggbb" hex color component.
_HEX_BASE = 16


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
