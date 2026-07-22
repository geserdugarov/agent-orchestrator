# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable dashboard widget surface backed by focused component leaves."""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from orchestrator import _dashboard_compatibility as compatibility
from orchestrator import _dashboard_widget_costs as costs
from orchestrator import _dashboard_widget_models as models
from orchestrator import _dashboard_widget_pipeline as pipeline
from orchestrator import _dashboard_widget_runs as runs
from orchestrator import _dashboard_widget_skills as skills
from orchestrator import _dashboard_widget_states as states
from orchestrator import _dashboard_widget_usage as usage
from orchestrator import dashboard_kpi_strip as kpi_strip


PLOTLY_CONFIG: Mapping[str, Any] = MappingProxyType({"displayModeBar": False})
NO_DATA_MESSAGE = states.NO_DATA_MESSAGE
EMPTY_WINDOW_MESSAGE = states.EMPTY_WINDOW_MESSAGE
NO_AGENT_EXITS_MESSAGE = costs.NO_AGENT_EXITS_MESSAGE
_TABLE_ROW_HEIGHT = costs._TABLE_ROW_HEIGHT
_TABLE_BASE_HEIGHT = costs._TABLE_BASE_HEIGHT
_KpiInputs = kpi_strip._KpiInputs
_DashboardModules = models._DashboardModules
_DashboardFilters = models._DashboardFilters
_DashboardControls = models._DashboardControls
_DashboardPage = models._DashboardPage
_DashboardKpis = models._DashboardKpis
_LoadedDashboard = models._LoadedDashboard
_ReliabilityPanelData = models._ReliabilityPanelData
_render_topbar_and_meta = pipeline._render_topbar_and_meta
_render_dashboard_insights = pipeline._render_dashboard_insights
_render_first_wave = pipeline._render_first_wave
_load_dashboard_data = pipeline._load_dashboard_data
_render_chart_widgets = pipeline._render_chart_widgets
_render_remaining_widgets = pipeline._render_remaining_widgets
_render_dashboard_widgets = pipeline._render_dashboard_widgets
_render_dashboard_footer = states._render_dashboard_footer
_render_no_data = states._render_no_data
_render_empty_window = states._render_empty_window
_backend_tokens_by_day = usage._backend_tokens_by_day
_stack_mode_label = usage._stack_mode_label
_stack_mode_index = usage._stack_mode_index
_render_hero_usage = usage._render_hero_usage
_render_stage_review_bars = costs._render_stage_review_bars
_paired_bars_height = costs._paired_bars_height
_render_issues_and_backends = costs._render_issues_and_backends
_render_repo_and_reliability = costs._render_repo_and_reliability
_render_activity_heatmap = costs._render_activity_heatmap
_render_skill_adoption = skills._render_skill_adoption
_skill_adoption_zero_caption = skills._skill_adoption_zero_caption
_skill_adoption_evidence_caption = skills._skill_adoption_evidence_caption
_render_skill_invocation_diagnostics = skills._render_skill_invocation_diagnostics
_render_skill_triggers = skills._render_skill_triggers
_render_skill_matrix_expander = skills._render_skill_matrix_expander
_render_recent_runs = runs._render_recent_runs
_render_drilldown_view = runs._render_drilldown_view

_COMPATIBILITY_MEMBERS = (
    _DashboardModules,
    _DashboardFilters,
    _DashboardControls,
    _DashboardPage,
    _DashboardKpis,
    _LoadedDashboard,
    _ReliabilityPanelData,
    _render_topbar_and_meta,
    _render_dashboard_insights,
    _render_first_wave,
    _load_dashboard_data,
    _render_chart_widgets,
    _render_remaining_widgets,
    _render_dashboard_widgets,
    _render_dashboard_footer,
    _render_no_data,
    _render_empty_window,
    _backend_tokens_by_day,
    _stack_mode_label,
    _stack_mode_index,
    _render_hero_usage,
    _render_stage_review_bars,
    _paired_bars_height,
    _render_issues_and_backends,
    _render_repo_and_reliability,
    _render_activity_heatmap,
    _render_skill_adoption,
    _skill_adoption_zero_caption,
    _skill_adoption_evidence_caption,
    _render_skill_invocation_diagnostics,
    _render_skill_triggers,
    _render_skill_matrix_expander,
    _render_recent_runs,
    _render_drilldown_view,
)
compatibility.preserve_defining_module(__name__, _COMPATIBILITY_MEMBERS)
