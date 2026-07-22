# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable cost-chart surface backed by focused chart leaves."""
from __future__ import annotations

from orchestrator import _dashboard_compatibility as compatibility
from orchestrator import _dashboard_cost_horizontal as horizontal
from orchestrator import _dashboard_cost_layout as layout
from orchestrator import _dashboard_cost_repo as repository
from orchestrator import _dashboard_cost_review as review
from orchestrator import _dashboard_cost_stage as stage


_REVIEW_BAR_ROW_HEIGHT = review.REVIEW_BAR_ROW_HEIGHT
_REVIEW_BAR_EXTRA_HEIGHT = review.REVIEW_BAR_EXTRA_HEIGHT
_HORIZONTAL_BAR_MARGIN = layout.HORIZONTAL_BAR_MARGIN
_REVIEW_ROUND_LABELS = review.REVIEW_ROUND_LABELS
_REVIEW_ROUND_ORDER = review.REVIEW_ROUND_ORDER
_DEFAULT_CHART_HEIGHT = horizontal.DEFAULT_CHART_HEIGHT
_CACHE_LIGHTEN = stage.CACHE_LIGHTEN
_HEX_BASE = stage.HEX_BASE
_HorizontalCostLayout = layout._HorizontalCostLayout
_apply_horizontal_cost_layout = layout._apply_horizontal_cost_layout
_CostBarTrace = layout._CostBarTrace
_cost_bar_trace = layout._cost_bar_trace
_HorizontalBars = horizontal._HorizontalBars
_reverse_horizontal_bars = horizontal._reverse_horizontal_bars
_horizontal_bars_data = horizontal._horizontal_bars_data
_cost_item_sort_key = horizontal._cost_item_sort_key
cost_horizontal_bars = horizontal.cost_horizontal_bars
_StageCostBars = stage._StageCostBars
_stage_no_cache_cost = stage._stage_no_cache_cost
_reverse_stage_cost_bars = stage._reverse_stage_cost_bars
_stage_cost_bars = stage._stage_cost_bars
cost_by_stage = stage.cost_by_stage
_lighten_hex = stage._lighten_hex
_ReviewCostBars = review._ReviewCostBars
_developer_cost_total = review._developer_cost_total
_reviewer_cost_total = review._reviewer_cost_total
_reverse_review_cost_bars = review._reverse_review_cost_bars
_review_cost_bars = review._review_cost_bars
_review_cost_traces = review._review_cost_traces
cost_by_review_round = review.cost_by_review_round
cost_by_repo = repository.cost_by_repo
_repo_short_name = repository._repo_short_name

_PUBLIC_BUILDERS = (
    cost_horizontal_bars,
    cost_by_stage,
    cost_by_review_round,
    cost_by_repo,
)
compatibility.preserve_defining_module(__name__, _PUBLIC_BUILDERS)
