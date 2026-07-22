# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable lazy-export inventory for analytics reads."""

from __future__ import annotations

from orchestrator._compat_exports import export_group

EXPORTS = (
    *export_group(
        "orchestrator.analytics.connection",
        (
            ("AnalyticsReadError", "AnalyticsReadError"),
            ("analytics_connection", "analytics_connection"),
            ("close_thread_local_connection", "close_thread_local_connection"),
            ("_close_quietly", "_close_quietly"),
            ("_default_connect", "_default_connect"),
            ("_default_persistent_connect", "_default_persistent_connect"),
            ("_is_broken_connection_exc", "_is_broken_connection_exc"),
            ("_thread_local", "_thread_local"),
        ),
    ),
    *export_group(
        "orchestrator.analytics.read_dashboard",
        (
            ("get_backend_daily_tokens", "get_backend_daily_tokens"),
            ("get_cost_coverage", "get_cost_coverage"),
            ("get_hourly_heatmap", "get_hourly_heatmap"),
            ("get_review_round_breakdown", "get_review_round_breakdown"),
            ("get_skill_adoption", "get_skill_adoption"),
            ("get_skill_trigger_matrix", "get_skill_trigger_matrix"),
            ("get_skill_trigger_rates", "get_skill_trigger_rates"),
        ),
    ),
    *export_group(
        "orchestrator.analytics.read_models_activity",
        (
            ("BackendDailyTokensRow", "BackendDailyTokensRow"),
            ("HourlyHeatmapPoint", "HourlyHeatmapPoint"),
            ("ThroughputDayRow", "ThroughputDayRow"),
        ),
    ),
    *export_group(
        "orchestrator.analytics.read_models_core",
        (
            ("DataExtent", "DataExtent"),
            ("FilterOptions", "FilterOptions"),
            ("Summary", "Summary"),
            ("TimeSeriesPoint", "TimeSeriesPoint"),
        ),
    ),
    *export_group(
        "orchestrator.analytics.read_models_cost",
        (
            ("BackendEfficiencyRow", "BackendEfficiencyRow"),
            ("CostCoverageRow", "CostCoverageRow"),
            ("RepoBreakdownRow", "RepoBreakdownRow"),
            ("ReviewRoundBucketRow", "ReviewRoundBucketRow"),
        ),
    ),
    *export_group(
        "orchestrator.analytics.read_models_runs",
        (
            ("AgentExitRow", "AgentExitRow"),
            ("EventBreakdown", "EventBreakdown"),
            ("IssueEventRow", "IssueEventRow"),
            ("IssueSummaryRow", "IssueSummaryRow"),
            ("StageBreakdown", "StageBreakdown"),
        ),
    ),
    *export_group(
        "orchestrator.analytics.read_models_skills",
        (
            ("SkillAdoptionRow", "SkillAdoptionRow"),
            ("SkillTriggerMatrixRow", "SkillTriggerMatrixRow"),
            ("SkillTriggerRateRow", "SkillTriggerRateRow"),
        ),
    ),
    *export_group(
        "orchestrator.analytics.read_raw",
        (
            ("SORT_BY_COST", "SORT_BY_COST"),
            ("SORT_BY_LAST_SEEN", "SORT_BY_LAST_SEEN"),
            ("get_data_extent", "get_data_extent"),
            ("get_event_breakdown", "get_event_breakdown"),
            ("get_filter_options", "get_filter_options"),
            ("get_issue_events", "get_issue_events"),
            ("get_issues", "get_issues"),
            ("get_recent_agent_exits", "get_recent_agent_exits"),
        ),
    ),
    *export_group(
        "orchestrator.analytics.read_rollup",
        (
            ("get_backend_efficiency", "get_backend_efficiency"),
            ("get_kpi_prev", "get_kpi_prev"),
            ("get_repo_breakdown", "get_repo_breakdown"),
            ("get_stage_breakdown", "get_stage_breakdown"),
            ("get_summary", "get_summary"),
            ("get_throughput_breakdown", "get_throughput_breakdown"),
            ("get_time_series", "get_time_series"),
        ),
    ),
)

EXPORTED_NAMES = tuple(sorted(target.export_name for target in EXPORTS))
