# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Daily token and cost rollups for usage charts."""
from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from orchestrator.analytics.read import TimeSeriesPoint
from orchestrator import _dashboard_usage_models as models


INPUT_BAND = "input"
OUTPUT_BAND = "output"
CACHE_BAND = "cache"
COST_BAND = "cost"
BACKEND_MODE = "backend"


def _empty_token_bucket() -> dict[str, float]:
    """Return a fresh zeroed token and cost accumulator."""
    return {
        INPUT_BAND: float(),
        OUTPUT_BAND: float(),
        CACHE_BAND: float(),
        COST_BAND: float(),
    }


def _date_axis(days: Sequence[date]) -> list:
    return list(days)


def _roll_up_time_series(
    points: Sequence[TimeSeriesPoint],
) -> models.DailyTokenValues:
    daily: models.DailyTokenValues = {}
    for point in points:
        bucket = daily.setdefault(point.day, _empty_token_bucket())
        bucket[INPUT_BAND] += float(point.input_tokens or 0)
        bucket[OUTPUT_BAND] += float(point.output_tokens or 0)
        bucket[CACHE_BAND] += float(
            (point.cache_read_tokens or 0) + (point.cache_write_tokens or 0)
        )
        bucket[COST_BAND] += float(point.cost_usd or 0)
    return daily


def _ensure_backend_days(
    daily: models.DailyTokenValues,
    backend_rows_by_day: models.DailyTokenValues,
) -> None:
    for day in backend_rows_by_day:
        daily.setdefault(day, _empty_token_bucket())


def _backend_names(
    backend_rows_by_day: models.DailyTokenValues,
) -> list[str]:
    return sorted(
        {
            backend
            for by_backend in backend_rows_by_day.values()
            for backend in by_backend
        }
    )


def _usage_stack_totals(
    days: Sequence[date],
    daily: models.DailyTokenValues,
    *,
    backend_rows_by_day: Optional[models.DailyTokenValues],
    mode: str,
) -> list[float]:
    if mode == BACKEND_MODE and backend_rows_by_day:
        return [sum(backend_rows_by_day.get(day, {}).values()) for day in days]
    return [_daily_token_total(daily[day]) for day in days]


def _daily_token_total(bucket: dict[str, float]) -> float:
    return sum(
        bucket[token_type]
        for token_type in (INPUT_BAND, OUTPUT_BAND, CACHE_BAND)
    )
