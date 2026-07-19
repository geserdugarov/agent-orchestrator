# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""KPI-strip computation for the analytics dashboard.

Home of the token / throughput / rework aggregations: the helpers that turn
a `Summary` aggregate plus the first-wave read rows (`_summary_total_tokens`
... `_build_kpi_strip_data`) into the four KPI-tile dicts and the resolved /
rejected throughput totals.

Pure functions over read-model rows and the `dashboard_kpis` delta /
rework math; no Streamlit / Plotly / pandas import, so the module stays
off the polling tick's dependency footprint. The widget-rendering
pipeline in `orchestrator.dashboard_widgets` imports `_KpiInputs` from here
and reaches `_build_kpi_strip_data` through the `orchestrator.dashboard`
facade one-directionally.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from orchestrator.analytics.read import Summary
from orchestrator.dashboard_kpis import kpi_delta, rework_totals

# The KPI-strip payload: the four KPI-card dicts plus the resolved /
# rejected throughput totals `_render_first_wave` folds into a
# `_DashboardKpis`. Named so `_build_kpi_strip_data`'s return annotation
# stays shallow.
_KpiStripData = tuple[list[dict[str, Any]], int, int]


def _summary_total_tokens(summary: Summary) -> int:
    """Return the dashboard token total used by KPIs and sparklines.

    Cache read/write tokens are counted with input/output so the KPI
    total matches the hero chart's Cache band. The cumulative
    `cached_tokens` field is intentionally excluded to avoid double
    counting reused prompt slices.
    """
    return int(
        (summary.total_input_tokens or 0)
        + (summary.total_output_tokens or 0)
        + (summary.total_cache_read_tokens or 0)
        + (summary.total_cache_write_tokens or 0)
    )


def _time_series_total_tokens(point: Any) -> float:
    return float(
        (point.input_tokens or 0)
        + (point.output_tokens or 0)
        + (point.cache_read_tokens or 0)
        + (point.cache_write_tokens or 0)
    )


def _throughput_totals(throughput_rows: Sequence[Any]) -> tuple[int, int]:
    resolved = sum(int(row.resolved or 0) for row in throughput_rows)
    rejected = sum(int(row.rejected or 0) for row in throughput_rows)
    return resolved, rejected


def _daily_point_totals(
    ts_points: Sequence[Any],
) -> dict[date, list[float]]:
    totals: dict[date, list[float]] = {}
    for point in ts_points:
        daily = totals.setdefault(point.day, [0.0, 0.0])
        daily[0] += float(point.cost_usd or 0)
        daily[1] += _time_series_total_tokens(point)
    return totals


@dataclass(frozen=True)
class _DailyKpiSeries:
    cost: Sequence[float]
    tokens: Sequence[float]
    done: Sequence[int]


def _daily_kpi_series(
    *, ts_points: Sequence[Any], throughput_rows: Sequence[Any]
) -> _DailyKpiSeries:
    """Return cost/token/resolved sparkline series for KPI cards.

    One entry is emitted per day present in the time-series read. Daily
    tokens use the same input + output + cache_read + cache_write
    accounting as the headline token KPI.
    """
    totals = _daily_point_totals(ts_points)
    days = sorted(totals)
    done_index = {row.day: int(row.resolved or 0) for row in throughput_rows}
    return _DailyKpiSeries(
        cost=[totals[day][0] for day in days],
        tokens=[totals[day][1] for day in days],
        done=[done_index.get(day, 0) for day in days],
    )


@dataclass(frozen=True)
class _KpiInputs:
    theme: Any
    summary: Summary
    prev_summary: Summary
    ts_points: Sequence[Any]
    throughput_rows: Sequence[Any]
    review_round_rows: Sequence[Any]
    days_in_window: int


@dataclass(frozen=True)
class _KpiTotals:
    cost: float
    tokens: int
    previous_cost: float
    previous_tokens: int
    resolved: int
    rejected: int
    review_cost: float
    rework_cost: float


def _kpi_totals(inputs: _KpiInputs) -> _KpiTotals:
    throughput = _throughput_totals(inputs.throughput_rows)
    review_costs = rework_totals(inputs.review_round_rows)
    return _KpiTotals(
        cost=float(inputs.summary.total_cost_usd or 0),
        tokens=_summary_total_tokens(inputs.summary),
        previous_cost=float(inputs.prev_summary.total_cost_usd or 0),
        previous_tokens=_summary_total_tokens(inputs.prev_summary),
        resolved=throughput[0],
        rejected=throughput[1],
        review_cost=review_costs[0],
        rework_cost=review_costs[1],
    )


def _cost_per_resolved(totals: _KpiTotals) -> str:
    if totals.resolved <= 0:
        return "—"
    avg_cost = totals.cost / totals.resolved
    return f"${avg_cost:,.2f}"


def _kpi_strip_entries(
    inputs: _KpiInputs,
    totals: _KpiTotals,
    daily: _DailyKpiSeries,
    rework_share: float,
) -> list[dict[str, Any]]:
    daily_cost = inputs.theme.fmt_money(totals.cost / inputs.days_in_window)
    daily_tokens = inputs.theme.fmt_tokens(totals.tokens / inputs.days_in_window)
    rework_pct = rework_share * 100
    rework_cost = inputs.theme.fmt_money_exact(totals.rework_cost)
    return [
        {
            "label": "Total spend",
            "value": inputs.theme.fmt_money_exact(totals.cost),
            "delta": kpi_delta(totals.cost, totals.previous_cost),
            "sub": f"{daily_cost}/day",
            "spark": daily.cost,
            "spark_color": inputs.theme.ACCENT,
        },
        {
            "label": "Total tokens",
            "value": inputs.theme.fmt_tokens(totals.tokens),
            "delta": kpi_delta(totals.tokens, totals.previous_tokens),
            "sub": f"{daily_tokens}/day",
            "spark": daily.tokens,
            "spark_color": inputs.theme.TOKEN_TYPE_COLORS["Input"],
        },
        {
            "label": "Cost / resolved issue",
            "value": _cost_per_resolved(totals),
            "delta": None,
            "sub": f"{totals.resolved} resolved · {totals.rejected} rejected",
            "spark": daily.done,
            "spark_color": inputs.theme.TOKEN_TYPE_COLORS["Cache"],
        },
        {
            "label": "Rework share",
            "value": f"{rework_pct:.0f}%",
            "delta": None,
            "sub": f"{rework_cost} in review rounds >= 1",
            "spark": None,
        },
    ]


def _build_kpi_strip_data(
    inputs: _KpiInputs,
) -> _KpiStripData:
    """Build the KPI-strip dictionaries plus throughput totals."""
    totals = _kpi_totals(inputs)
    rework_share = (
        (totals.rework_cost / totals.review_cost)
        if totals.review_cost > 0
        else 0.0
    )
    daily = _daily_kpi_series(
        ts_points=inputs.ts_points,
        throughput_rows=inputs.throughput_rows,
    )
    kpis = _kpi_strip_entries(inputs, totals, daily, rework_share)
    return kpis, totals.resolved, totals.rejected
