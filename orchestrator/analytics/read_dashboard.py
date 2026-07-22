# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard analytics readers backed by focused query families."""

from __future__ import annotations

from typing import Any

from orchestrator.analytics._read_dashboard_breakdowns import (
    _backend_daily_token_rows,
    _cost_coverage_rows,
    _hourly_heatmap_rows,
)
from orchestrator.analytics._read_review_rounds import _review_round_rows
from orchestrator.analytics._read_row_values import _cost_cell as _cost_cell
from orchestrator.analytics._read_skill_adoption import (
    SKILL_ADOPTION_ROW_LIMIT as SKILL_ADOPTION_ROW_LIMIT,
    _skill_adoption_rows,
)
from orchestrator.analytics._read_skill_matrix import (
    SKILL_MATRIX_ROW_LIMIT as SKILL_MATRIX_ROW_LIMIT,
    _skill_trigger_matrix_rows,
)
from orchestrator.analytics._read_skill_trigger_rates import (
    _skill_trigger_rate_rows,
)
from orchestrator.analytics.predicates import _agent_event_excluded
from orchestrator.analytics.read_models import (
    BackendDailyTokensRow,
    CostCoverageRow,
    HourlyHeatmapPoint,
    ReviewRoundBucketRow,
    SkillAdoptionRow,
    SkillTriggerMatrixRow,
    SkillTriggerRateRow,
)
from orchestrator.analytics.read_request import (
    FILTERED_READ_SIGNATURE,
    HEATMAP_SIGNATURE,
    LIMITED_READ_SIGNATURE,
    bind_read_request,
    resolve_read_query,
    window_filters,
)


_COMPATIBILITY_EXPORTS = (
    _cost_cell,
    SKILL_ADOPTION_ROW_LIMIT,
    SKILL_MATRIX_ROW_LIMIT,
)


_REVIEW_ROUND_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="list[ReviewRoundBucketRow]",
)
_SKILL_TRIGGER_RATE_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="list[SkillTriggerRateRow]",
)
_SKILL_TRIGGER_MATRIX_SIGNATURE = LIMITED_READ_SIGNATURE.replace(
    return_annotation="list[SkillTriggerMatrixRow]",
)
_SKILL_ADOPTION_SIGNATURE = LIMITED_READ_SIGNATURE.replace(
    return_annotation="list[SkillAdoptionRow]",
)
_COST_COVERAGE_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="list[CostCoverageRow]",
)
_BACKEND_DAILY_TOKENS_SIGNATURE = FILTERED_READ_SIGNATURE.replace(
    return_annotation="list[BackendDailyTokensRow]",
)
_HOURLY_HEATMAP_SIGNATURE = HEATMAP_SIGNATURE.replace(
    return_annotation="list[HourlyHeatmapPoint]",
)


def get_review_round_breakdown(
    *args: Any,
    **kwargs: Any,
) -> list[ReviewRoundBucketRow]:
    """Return per-review-round development and review cost buckets."""
    request = bind_read_request(_REVIEW_ROUND_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    if _agent_event_excluded(request.filters.events):
        return []
    return _review_round_rows(query, window_filters(request))


get_review_round_breakdown.__signature__ = _REVIEW_ROUND_SIGNATURE


def get_skill_trigger_rates(
    *args: Any,
    **kwargs: Any,
) -> list[SkillTriggerRateRow]:
    """Return skill-trigger rates grouped by agent role and backend."""
    request = bind_read_request(_SKILL_TRIGGER_RATE_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    if _agent_event_excluded(request.filters.events):
        return []
    return _skill_trigger_rate_rows(query, window_filters(request))


get_skill_trigger_rates.__signature__ = _SKILL_TRIGGER_RATE_SIGNATURE


def get_skill_trigger_matrix(
    *args: Any,
    **kwargs: Any,
) -> list[SkillTriggerMatrixRow]:
    """Return per-skill trigger cells for each repository cohort."""
    request = bind_read_request(_SKILL_TRIGGER_MATRIX_SIGNATURE, args, kwargs)
    selected_limit = int(request.options.limit or 0)
    query = resolve_read_query(request)
    if not query.available:
        return []
    if _agent_event_excluded(request.filters.events):
        return []
    return _skill_trigger_matrix_rows(
        query,
        window_filters(request),
        selected_limit,
    )


get_skill_trigger_matrix.__signature__ = _SKILL_TRIGGER_MATRIX_SIGNATURE


def get_skill_adoption(*args: Any, **kwargs: Any) -> list[SkillAdoptionRow]:
    """Return per-session skill adoption cells for each repository cohort."""
    request = bind_read_request(_SKILL_ADOPTION_SIGNATURE, args, kwargs)
    selected_limit = int(request.options.limit or 0)
    query = resolve_read_query(request)
    if not query.available:
        return []
    if _agent_event_excluded(request.filters.events):
        return []
    return _skill_adoption_rows(
        query,
        window_filters(request),
        selected_limit,
    )


get_skill_adoption.__signature__ = _SKILL_ADOPTION_SIGNATURE


def get_cost_coverage(*args: Any, **kwargs: Any) -> list[CostCoverageRow]:
    """Return token-volume coverage grouped by cost source."""
    request = bind_read_request(_COST_COVERAGE_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    if _agent_event_excluded(request.filters.events):
        return []
    return _cost_coverage_rows(query, window_filters(request))


get_cost_coverage.__signature__ = _COST_COVERAGE_SIGNATURE


def get_backend_daily_tokens(
    *args: Any,
    **kwargs: Any,
) -> list[BackendDailyTokensRow]:
    """Return daily token totals grouped by backend."""
    request = bind_read_request(_BACKEND_DAILY_TOKENS_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    if _agent_event_excluded(request.filters.events):
        return []
    return _backend_daily_token_rows(query, window_filters(request))


get_backend_daily_tokens.__signature__ = _BACKEND_DAILY_TOKENS_SIGNATURE


def get_hourly_heatmap(
    *args: Any,
    **kwargs: Any,
) -> list[HourlyHeatmapPoint]:
    """Return weekday-by-hour activity cells in the requested timezone."""
    request = bind_read_request(_HOURLY_HEATMAP_SIGNATURE, args, kwargs)
    query = resolve_read_query(request)
    if not query.available:
        return []
    return _hourly_heatmap_rows(
        query,
        window_filters(request),
        request.options.tz_offset_hours,
    )


get_hourly_heatmap.__signature__ = _HOURLY_HEATMAP_SIGNATURE
