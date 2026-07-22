# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard date-window and preset calculations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from orchestrator import _dashboard_state_constants as constants
from orchestrator.analytics.read import DataExtent


@dataclass(frozen=True)
class DateWindow:
    start: datetime
    end: datetime


def default_date_range(
    *,
    today: Optional[date] = None,
    days: int = constants.DEFAULT_WINDOW_DAYS,
) -> tuple[date, date]:
    range_end = today or date.today()
    range_start = range_end - timedelta(days=max(days - 1, 0))
    return range_start, range_end


def to_window(start_date: date, end_date: date) -> DateWindow:
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    start_datetime = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_datetime = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=timezone.utc,
    )
    return DateWindow(start=start_datetime, end=end_datetime)


def extent_dates(extent: DataExtent) -> Optional[tuple[date, date]]:
    if extent.min_ts is None or extent.max_ts is None:
        return None
    return extent.min_ts.date(), extent.max_ts.date()


def preset_window(
    preset: str,
    extent: DataExtent,
) -> Optional[DateWindow]:
    bounds = extent_dates(extent)
    if bounds is None:
        return None
    minimum_date, maximum_date = bounds
    if preset == constants.PRESET_ALL:
        return to_window(minimum_date, maximum_date)
    days = constants.PRESET_DAYS.get(preset)
    if days is None:
        return None
    start_date = max(
        maximum_date - timedelta(days=days - 1),
        minimum_date,
    )
    return to_window(start_date, maximum_date)


def previous_window(window: DateWindow) -> DateWindow:
    window_length = window.end - window.start
    return DateWindow(
        start=window.start - window_length,
        end=window.start,
    )
