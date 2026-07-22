# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable analytics read-model imports grouped by result family."""

from __future__ import annotations

from orchestrator.analytics.read_models_activity import (
    BackendDailyTokensRow as BackendDailyTokensRow,
    HourlyHeatmapPoint as HourlyHeatmapPoint,
    ThroughputDayRow as ThroughputDayRow,
)
from orchestrator.analytics.read_models_core import (
    DataExtent as DataExtent,
    FilterOptions as FilterOptions,
    Summary as Summary,
    TimeSeriesPoint as TimeSeriesPoint,
)
from orchestrator.analytics.read_models_cost import (
    BackendEfficiencyRow as BackendEfficiencyRow,
    CostCoverageRow as CostCoverageRow,
    RepoBreakdownRow as RepoBreakdownRow,
    ReviewRoundBucketRow as ReviewRoundBucketRow,
)
from orchestrator.analytics.read_models_runs import (
    AgentExitRow as AgentExitRow,
    EventBreakdown as EventBreakdown,
    IssueEventRow as IssueEventRow,
    IssueSummaryRow as IssueSummaryRow,
    StageBreakdown as StageBreakdown,
)
from orchestrator.analytics.read_models_skills import (
    SkillAdoptionRow as SkillAdoptionRow,
    SkillTriggerMatrixRow as SkillTriggerMatrixRow,
    SkillTriggerRateRow as SkillTriggerRateRow,
)


_COMPATIBILITY_EXPORTS = (
    BackendDailyTokensRow,
    HourlyHeatmapPoint,
    ThroughputDayRow,
    DataExtent,
    FilterOptions,
    Summary,
    TimeSeriesPoint,
    BackendEfficiencyRow,
    CostCoverageRow,
    RepoBreakdownRow,
    ReviewRoundBucketRow,
    AgentExitRow,
    EventBreakdown,
    IssueEventRow,
    IssueSummaryRow,
    StageBreakdown,
    SkillAdoptionRow,
    SkillTriggerMatrixRow,
    SkillTriggerRateRow,
)
