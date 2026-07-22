# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard backend, repository, and activity read wrappers."""
from __future__ import annotations

from orchestrator.analytics import read as analytics_read
from orchestrator._dashboard_read_core import _read_filtered


def _read_backend_efficiency(key: tuple):
    return _read_filtered(analytics_read.get_backend_efficiency, key)


def _read_repo_breakdown(key: tuple):
    return _read_filtered(analytics_read.get_repo_breakdown, key)


def _read_cost_coverage(key: tuple):
    return _read_filtered(analytics_read.get_cost_coverage, key)


def _read_hourly_heatmap(key: tuple, tz_offset_hours: int):
    return _read_filtered(
        analytics_read.get_hourly_heatmap,
        key,
        tz_offset_hours=tz_offset_hours,
    )


def _read_throughput(key: tuple):
    return _read_filtered(analytics_read.get_throughput_breakdown, key)


def _read_backend_daily_tokens(key: tuple):
    return _read_filtered(analytics_read.get_backend_daily_tokens, key)


def _read_skill_trigger_rates(key: tuple):
    return _read_filtered(analytics_read.get_skill_trigger_rates, key)
