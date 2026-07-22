# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed data shared by dashboard usage-chart components."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence


DailyTokenValues = dict[date, dict[str, float]]


@dataclass(frozen=True)
class _UsageChartData:
    daily: DailyTokenValues
    days: Sequence[date]


@dataclass(frozen=True)
class _UsageAxisRanges:
    token_top: float
    cost_top: float
