# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard window, timezone, and read-mode constants."""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


DEFAULT_WINDOW_DAYS = 7
PRESET_RECENT_THREE_DAYS = "3d"
PRESET_RECENT_WEEK = "7d"
PRESET_ALL = "All"
PRESET_CUSTOM = "Custom"
PRESET_OPTIONS = (
    PRESET_RECENT_THREE_DAYS,
    PRESET_RECENT_WEEK,
    PRESET_ALL,
    PRESET_CUSTOM,
)
PRESET_LABELS: Mapping[str, str] = MappingProxyType({
    PRESET_RECENT_THREE_DAYS: "Last 3 days",
    PRESET_RECENT_WEEK: "Last 7 days",
    PRESET_ALL: "All time",
    PRESET_CUSTOM: "Custom range",
})
PRESET_INLINE_LABELS: Mapping[str, str] = MappingProxyType({
    PRESET_RECENT_THREE_DAYS: "3D",
    PRESET_RECENT_WEEK: "7D",
    PRESET_ALL: "All",
})
PRESET_DAYS: Mapping[str, int] = MappingProxyType({
    PRESET_RECENT_THREE_DAYS: 3,
    PRESET_RECENT_WEEK: 7,
})
DEFAULT_PRESET = PRESET_RECENT_WEEK
MIN_UTC_OFFSET = -12
MAX_UTC_OFFSET = 14
TZ_OFFSET_OPTIONS = tuple(range(MIN_UTC_OFFSET, MAX_UTC_OFFSET + 1))
DEFAULT_TZ_OFFSET_HOURS = 7
PARALLEL_READS_ENV = "DASHBOARD_PARALLEL_READS"
PARALLEL_READS_MAX_WORKERS = 8
TRUTHY = frozenset(("1", "true", "on", "yes"))
UNCONFIGURED_DB_MESSAGE = (
    "`ANALYTICS_DB_URL` is not configured. Set it in your environment "
    "(see `.env.example.advanced` and `docs/configuration.md`) and "
    "reload the dashboard to view analytics."
)
