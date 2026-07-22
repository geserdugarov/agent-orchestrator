# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable dashboard card surface backed by focused card leaves."""
from __future__ import annotations

from orchestrator import _dashboard_backend_card as backend
from orchestrator import _dashboard_card_headers as headers
from orchestrator import _dashboard_coverage_card as coverage


_card_header_html = headers.card_header_html
_insights_html = headers.insights_html
_backend_efficiency_card_html = backend.backend_efficiency_card_html
_BackendEfficiencyMetrics = backend.BackendEfficiencyMetrics
_safe_ratio = backend.safe_ratio
_backend_efficiency_metrics = backend.backend_efficiency_metrics
_cost_coverage_weights = coverage.cost_coverage_weights
_cost_source_color = coverage.cost_source_color
_CoverageSegment = coverage.CoverageSegment
_coverage_segment = coverage.coverage_segment
_coverage_segments = coverage.coverage_segments
_cost_coverage_bar_html = coverage.cost_coverage_bar_html
_reliability_tiles_html = headers.reliability_tiles_html

_card_header_html.__module__ = __name__
_insights_html.__module__ = __name__
_backend_efficiency_card_html.__module__ = __name__
_cost_coverage_bar_html.__module__ = __name__
_reliability_tiles_html.__module__ = __name__
