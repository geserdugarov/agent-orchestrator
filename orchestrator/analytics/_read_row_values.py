# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed scalar access at analytics row boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence


def _row_value(row: Sequence[Any], index: int, default: Any = 0) -> Any:
    if len(row) <= index:
        return default
    return row[index]


def _cost_cell(row: Sequence[Any], index: int) -> float:
    """Read a nullable USD cost column as a float, treating null/missing as zero."""
    return float(_row_value(row, index) or 0)


def _day_value(day: Any) -> Any:
    if isinstance(day, datetime):
        return day.date()
    return day


def _float_or_none(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    return float(raw)
