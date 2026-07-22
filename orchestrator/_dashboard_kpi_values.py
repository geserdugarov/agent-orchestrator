# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard KPI totals and display-entry construction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from orchestrator.analytics.read import Summary
from orchestrator._dashboard_kpi_series import (
    _DailyKpiSeries,
    _daily_kpi_series,
    _summary_total_tokens,
    _throughput_totals,
)
from orchestrator.dashboard_kpis import kpi_delta, rework_totals


_LABEL_KEY = "label"
_VALUE_KEY = "value"
_DELTA_KEY = "delta"
_SUBTITLE_KEY = "sub"
_SPARK_KEY = "spark"
_KpiStripData = tuple[list[dict[str, Any]], int, int]


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
    return "${0}".format(format(totals.cost / totals.resolved, ",.2f"))


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
            _LABEL_KEY: "Total spend",
            _VALUE_KEY: inputs.theme.fmt_money_exact(totals.cost),
            _DELTA_KEY: kpi_delta(totals.cost, totals.previous_cost),
            _SUBTITLE_KEY: f"{daily_cost}/day",
            _SPARK_KEY: daily.cost,
            "spark_color": inputs.theme.ACCENT,
        },
        {
            _LABEL_KEY: "Total tokens",
            _VALUE_KEY: inputs.theme.fmt_tokens(totals.tokens),
            _DELTA_KEY: kpi_delta(totals.tokens, totals.previous_tokens),
            _SUBTITLE_KEY: f"{daily_tokens}/day",
            _SPARK_KEY: daily.tokens,
            "spark_color": inputs.theme.TOKEN_TYPE_COLORS["Input"],
        },
        {
            _LABEL_KEY: "Cost / resolved issue",
            _VALUE_KEY: _cost_per_resolved(totals),
            _DELTA_KEY: None,
            _SUBTITLE_KEY: (
                f"{totals.resolved} resolved · {totals.rejected} rejected"
            ),
            _SPARK_KEY: daily.done,
            "spark_color": inputs.theme.TOKEN_TYPE_COLORS["Cache"],
        },
        {
            _LABEL_KEY: "Rework share",
            _VALUE_KEY: f"{rework_pct:.0f}%",
            _DELTA_KEY: None,
            _SUBTITLE_KEY: f"{rework_cost} in review rounds >= 1",
            _SPARK_KEY: None,
        },
    ]


def _build_kpi_strip_data(inputs: _KpiInputs) -> _KpiStripData:
    """Build KPI dictionaries and throughput totals."""
    totals = _kpi_totals(inputs)
    rework_share = (
        totals.rework_cost / totals.review_cost
        if totals.review_cost > 0
        else float()
    )
    daily = _daily_kpi_series(
        ts_points=inputs.ts_points,
        throughput_rows=inputs.throughput_rows,
    )
    kpis = _kpi_strip_entries(inputs, totals, daily, rework_share)
    return kpis, totals.resolved, totals.rejected
