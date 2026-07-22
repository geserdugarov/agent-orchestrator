# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-repository cost-bar construction."""
from __future__ import annotations

from typing import Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme
from orchestrator import _dashboard_cost_horizontal as horizontal
from orchestrator.analytics.read import RepoBreakdownRow
from orchestrator.dashboard_charts_base import _empty_figure


def cost_by_repo(rows: Sequence[RepoBreakdownRow]) -> go.Figure:
    """Build per-repository cost bars."""
    if not rows:
        return _empty_figure(
            "No repos match the current filters.",
            height=horizontal.DEFAULT_CHART_HEIGHT,
        )
    bar_rows = [
        (
            _repo_short_name(row.repo),
            f"{int(row.agent_exits):,} runs",
            float(row.total_cost_usd or 0),
            theme.ACCENT,
        )
        for row in rows
    ]
    return horizontal.cost_horizontal_bars(bar_rows)


def _repo_short_name(repo: str) -> str:
    if "/" not in repo:
        return repo
    return repo.rsplit("/", maxsplit=1)[-1]
