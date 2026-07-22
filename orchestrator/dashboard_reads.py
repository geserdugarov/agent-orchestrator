# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable dashboard read surface backed by focused read leaves."""
from __future__ import annotations

from orchestrator import _dashboard_read_breakdowns as breakdowns
from orchestrator import _dashboard_compatibility as compatibility
from orchestrator import _dashboard_read_core as core
from orchestrator import _dashboard_read_dispatch as dispatch
from orchestrator import _dashboard_read_plan as plans
from orchestrator import _dashboard_read_rollups as rollups
from orchestrator import _dashboard_read_skills as skills


DEFAULT_RECENT_AGENT_EXITS = rollups.DEFAULT_RECENT_AGENT_EXITS
STATIC_METADATA_TTL_SECONDS = core.STATIC_METADATA_TTL_SECONDS
LOADING_INDICATOR_MESSAGE = dispatch.LOADING_INDICATOR_MESSAGE
_ReaderTask = plans._ReaderTask
_ReadResults = dispatch._ReadResults
_DashboardReadPlan = plans._DashboardReadPlan
_filter_list = core._filter_list
_scoped_read = core._scoped_read
_read_data_extent = core._read_data_extent
_read_filter_options = core._read_filter_options
_read_static_metadata = core._read_static_metadata
_read_filter_kwargs = core._read_filter_kwargs
_read_filtered = core._read_filtered
_read_summary = rollups._read_summary
_read_prev_kpi = rollups._read_prev_kpi
_read_time_series = rollups._read_time_series
_read_stage_breakdown = rollups._read_stage_breakdown
_read_recent_agent_exits = rollups._read_recent_agent_exits
_read_top_cost_issues = rollups._read_top_cost_issues
_read_review_round = rollups._read_review_round
_read_backend_efficiency = breakdowns._read_backend_efficiency
_read_repo_breakdown = breakdowns._read_repo_breakdown
_read_cost_coverage = breakdowns._read_cost_coverage
_read_hourly_heatmap = breakdowns._read_hourly_heatmap
_read_throughput = breakdowns._read_throughput
_read_backend_daily_tokens = breakdowns._read_backend_daily_tokens
_read_skill_trigger_rates = breakdowns._read_skill_trigger_rates
_read_skill_trigger_matrix = skills._read_skill_trigger_matrix
_read_skill_adoption = skills._read_skill_adoption
_widget_task = plans._widget_task
_first_wave_readers = plans._first_wave_readers
_second_wave_readers = plans._second_wave_readers
_widget_readers = plans._widget_readers
_build_read_keys = plans._build_read_keys
_dispatch_reads = dispatch._dispatch_reads
_log_dashboard_load = dispatch._log_dashboard_load
_run_read_waves = dispatch._run_read_waves
log = dispatch.log

_COMPATIBILITY_MEMBERS = (
    _DashboardReadPlan,
    _filter_list,
    _scoped_read,
    _read_data_extent,
    _read_filter_options,
    _read_static_metadata,
    _read_filter_kwargs,
    _read_filtered,
    _read_summary,
    _read_prev_kpi,
    _read_time_series,
    _read_stage_breakdown,
    _read_recent_agent_exits,
    _read_top_cost_issues,
    _read_review_round,
    _read_backend_efficiency,
    _read_repo_breakdown,
    _read_cost_coverage,
    _read_hourly_heatmap,
    _read_throughput,
    _read_backend_daily_tokens,
    _read_skill_trigger_rates,
    _read_skill_trigger_matrix,
    _read_skill_adoption,
    _widget_task,
    _first_wave_readers,
    _second_wave_readers,
    _widget_readers,
    _build_read_keys,
    _dispatch_reads,
    _log_dashboard_load,
    _run_read_waves,
)
compatibility.preserve_defining_module(__name__, _COMPATIBILITY_MEMBERS)
