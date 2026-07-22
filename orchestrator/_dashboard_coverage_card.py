# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Cost-attribution coverage card calculations and rendering."""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Sequence

from orchestrator.analytics.read import CostCoverageRow


@dataclass(frozen=True)
class CoverageSegment:
    bar_html: str
    legend: str


def cost_coverage_weights(
    rows: Sequence[CostCoverageRow],
) -> tuple[list[int], int]:
    total_tokens = sum(int(row.total_tokens or 0) for row in rows)
    if total_tokens > 0:
        return [int(row.total_tokens or 0) for row in rows], total_tokens
    weights = [int(row.runs or 0) for row in rows]
    return weights, sum(weights) or 1


def cost_source_color(
    cost_source: str,
    cost_sources: Sequence[str],
    theme,
) -> str:
    return theme.color_for(
        cost_source,
        cost_sources,
        explicit=theme.COST_SOURCE_COLORS,
    )


def coverage_segment(
    row: CostCoverageRow,
    weight: int,
    total: int,
    cost_sources: Sequence[str],
    theme,
) -> CoverageSegment:
    percentage = weight / total * 100
    color = cost_source_color(row.cost_source, cost_sources, theme)
    return CoverageSegment(
        bar_html=(
            f'<span style="width:{percentage:.1f}%;background:{color}" '
            f'title="{html.escape(row.cost_source)}"></span>'
        ),
        legend=(
            f'<span><span class="dot" style="background:{color}"></span>'
            f"{html.escape(row.cost_source)} "
            f'<b style="color:{theme.TEXT};'
            f'font-family:{theme.MONO_FONT_FAMILY}">{percentage:.1f}%</b>'
            "</span>"
        ),
    )


def coverage_segments(
    rows: Sequence[CostCoverageRow],
    weights: Sequence[int],
    total: int,
    cost_sources: Sequence[str],
    theme,
) -> list[CoverageSegment]:
    return [
        coverage_segment(row, weight, total, cost_sources, theme)
        for row, weight in zip(rows, weights)
    ]


def cost_coverage_bar_html(
    rows: Sequence[CostCoverageRow],
    *,
    theme,
) -> str:
    """Render the cost-attribution coverage bar to inline HTML."""
    weights, total = cost_coverage_weights(rows)
    segments = coverage_segments(
        rows,
        weights,
        total,
        [row.cost_source for row in rows],
        theme,
    )
    bars = "".join(segment.bar_html for segment in segments)
    legends = "".join(segment.legend for segment in segments)
    return (
        '<div class="orch-cov-title">Cost attribution coverage</div>'
        f'<div class="orch-cov-bar">{bars}</div>'
        f'<div class="orch-cov-legend">{legends}</div>'
    )
