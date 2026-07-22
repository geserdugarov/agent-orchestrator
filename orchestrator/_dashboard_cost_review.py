# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Development and review cost bars by review round."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional, Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator import _dashboard_cost_horizontal as horizontal
from orchestrator import _dashboard_cost_layout as cost_layout
from orchestrator import _dashboard_cost_stage as stage_cost
from orchestrator.analytics.read import ReviewRoundBucketRow
from orchestrator.dashboard_charts_base import (
    _empty_figure,
    _horizontal_legend,
    _reverse_lists,
    _two_line_y_ticks,
)


REVIEW_BAR_ROW_HEIGHT = 44
REVIEW_BAR_EXTRA_HEIGHT = 90
REVIEW_ROUND_LABELS = MappingProxyType({
    "0": "Initial",
    "1": "Round 1",
    "2": "Round 2",
    "3": "Round 3",
    "4": "Round 4",
    "5": "Round 5",
    "6+": "Rounds 6+",
    "unknown": "No review round",
})
REVIEW_ROUND_ORDER = ("0", "1", "2", "3", "4", "5", "6+", "unknown")


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
    return float(row.developer_no_cache_cost_usd or 0) + float(
        row.developer_cache_cost_usd or 0
    )


def _reviewer_cost_total(row: ReviewRoundBucketRow) -> float:
    return float(row.reviewer_no_cache_cost_usd or 0) + float(
        row.reviewer_cache_cost_usd or 0
    )


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
        for bucket in REVIEW_ROUND_ORDER
        if bucket in by_bucket
    ]
    if not ordered:
        return None
    return _reverse_review_cost_bars(
        _ReviewCostBars(
            labels=[REVIEW_ROUND_LABELS.get(row.bucket, row.bucket) for row in ordered],
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
        )
    )


def _review_cost_traces(
    bars: _ReviewCostBars,
    y_ticks: Sequence[str],
) -> tuple[cost_layout._CostBarTrace, ...]:
    developer_color = theme.AGENT_ROLE_COLORS["developer"]
    reviewer_color = theme.AGENT_ROLE_COLORS["reviewer"]
    developer_cache_color = stage_cost._lighten_hex(
        developer_color,
        stage_cost.CACHE_LIGHTEN,
    )
    reviewer_cache_color = stage_cost._lighten_hex(
        reviewer_color,
        stage_cost.CACHE_LIGHTEN,
    )
    return (
        cost_layout._CostBarTrace(
            name="Review (no cache)",
            amounts=bars.reviewer_no_cache,
            y_ticks=y_ticks,
            color=reviewer_color,
            offsetgroup="reviewer",
            hover_label="Review (no cache)",
        ),
        cost_layout._CostBarTrace(
            name="Review (cache)",
            amounts=bars.reviewer_cache,
            y_ticks=y_ticks,
            color=reviewer_cache_color,
            offsetgroup="reviewer",
            totals=bars.reviewer_totals,
            hover_label="Review (cache)",
        ),
        cost_layout._CostBarTrace(
            name="Development (no cache)",
            amounts=bars.developer_no_cache,
            y_ticks=y_ticks,
            color=developer_color,
            offsetgroup="developer",
            hover_label="Development (no cache)",
        ),
        cost_layout._CostBarTrace(
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
    rows: Sequence[ReviewRoundBucketRow],
    *,
    height: Optional[int] = None,
) -> go.Figure:
    """Build grouped cache and no-cache cost bars by review round."""
    if not rows:
        return _empty_figure(
            "No `agent_exit` rows match the current filters.",
            height=height or horizontal.DEFAULT_CHART_HEIGHT,
        )
    bars = _review_cost_bars(rows)
    if bars is None:
        return _empty_figure(
            "No development or review runs match the current filters.",
            height=height or horizontal.DEFAULT_CHART_HEIGHT,
        )
    y_ticks = _two_line_y_ticks(bars.labels, bars.subs)
    figure = go.Figure()
    for trace in _review_cost_traces(bars, y_ticks):
        figure.add_trace(cost_layout._cost_bar_trace(trace))
    cost_layout._apply_horizontal_cost_layout(
        figure,
        cost_layout._HorizontalCostLayout(
            row_count=len(y_ticks),
            height=height,
            barmode="relative",
            legend=_horizontal_legend(traceorder="reversed"),
            row_height=REVIEW_BAR_ROW_HEIGHT,
            extra_height=REVIEW_BAR_EXTRA_HEIGHT,
        ),
    )
    return figure
