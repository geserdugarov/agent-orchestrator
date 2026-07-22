# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Daily token, cost, and throughput series for dashboard KPIs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from orchestrator.analytics.read import Summary


def _summary_total_tokens(summary: Summary) -> int:
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


def _daily_point_totals(ts_points: Sequence[Any]) -> dict[date, list[float]]:
    totals: dict[date, list[float]] = {}
    for point in ts_points:
        daily = totals.setdefault(point.day, [float(), float()])
        daily[0] += float(point.cost_usd or 0)
        daily[1] += _time_series_total_tokens(point)
    return totals


@dataclass(frozen=True)
class _DailyKpiSeries:
    cost: Sequence[float]
    tokens: Sequence[float]
    done: Sequence[int]


def _daily_kpi_series(
    *,
    ts_points: Sequence[Any],
    throughput_rows: Sequence[Any],
) -> _DailyKpiSeries:
    totals = _daily_point_totals(ts_points)
    days = sorted(totals)
    done_index = {row.day: int(row.resolved or 0) for row in throughput_rows}
    return _DailyKpiSeries(
        cost=[totals[day][0] for day in days],
        tokens=[totals[day][1] for day in days],
        done=[done_index.get(day, 0) for day in days],
    )
