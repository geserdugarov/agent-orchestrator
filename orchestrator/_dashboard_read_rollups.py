# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard summary, cost, and lifecycle read wrappers."""
from __future__ import annotations

from orchestrator.analytics import read as analytics_read
from orchestrator._dashboard_read_core import _read_filtered
from orchestrator.dashboard_kpis import DEFAULT_EXPENSIVE_LIMIT


DEFAULT_RECENT_AGENT_EXITS = 100


def _read_summary(key: tuple):
    return _read_filtered(analytics_read.get_summary, key)


def _read_prev_kpi(key: tuple):
    return _read_filtered(analytics_read.get_kpi_prev, key)


def _read_time_series(key: tuple):
    return _read_filtered(analytics_read.get_time_series, key)


def _read_stage_breakdown(key: tuple):
    return _read_filtered(analytics_read.get_stage_breakdown, key)


def _read_recent_agent_exits(key: tuple):
    return _read_filtered(
        analytics_read.get_recent_agent_exits,
        key,
        limit=DEFAULT_RECENT_AGENT_EXITS,
    )


def _read_top_cost_issues(key: tuple):
    return _read_filtered(
        analytics_read.get_issues,
        key,
        limit=DEFAULT_EXPENSIVE_LIMIT,
        sort_by=analytics_read.SORT_BY_COST,
    )


def _read_review_round(key: tuple):
    return _read_filtered(analytics_read.get_review_round_breakdown, key)
