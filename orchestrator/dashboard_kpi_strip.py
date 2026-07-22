# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable dashboard KPI surface backed by focused KPI leaves."""
from __future__ import annotations

from orchestrator import _dashboard_compatibility as compatibility
from orchestrator import _dashboard_kpi_series as series
from orchestrator import _dashboard_kpi_values as kpi_values


_KpiStripData = kpi_values._KpiStripData
_summary_total_tokens = series._summary_total_tokens
_time_series_total_tokens = series._time_series_total_tokens
_throughput_totals = series._throughput_totals
_daily_point_totals = series._daily_point_totals
_DailyKpiSeries = series._DailyKpiSeries
_daily_kpi_series = series._daily_kpi_series
_KpiInputs = kpi_values._KpiInputs
_KpiTotals = kpi_values._KpiTotals
_kpi_totals = kpi_values._kpi_totals
_cost_per_resolved = kpi_values._cost_per_resolved
_kpi_strip_entries = kpi_values._kpi_strip_entries
_build_kpi_strip_data = kpi_values._build_kpi_strip_data

_COMPATIBILITY_MEMBERS = (
    _summary_total_tokens,
    _time_series_total_tokens,
    _throughput_totals,
    _daily_point_totals,
    _DailyKpiSeries,
    _daily_kpi_series,
    _KpiInputs,
    _KpiTotals,
    _kpi_totals,
    _cost_per_resolved,
    _kpi_strip_entries,
    _build_kpi_strip_data,
)
compatibility.preserve_defining_module(__name__, _COMPATIBILITY_MEMBERS)
