# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable dashboard state surface backed by focused state leaves."""
from __future__ import annotations

import sys

from orchestrator import _dashboard_filter_state as filter_state
from orchestrator import _dashboard_read_mode as read_mode
from orchestrator import _dashboard_state_constants as constants
from orchestrator import _dashboard_windows as windows


DEFAULT_WINDOW_DAYS = constants.DEFAULT_WINDOW_DAYS
PRESET_RECENT_THREE_DAYS = constants.PRESET_RECENT_THREE_DAYS
PRESET_RECENT_WEEK = constants.PRESET_RECENT_WEEK
setattr(sys.modules[__name__], "PRESET_3D", PRESET_RECENT_THREE_DAYS)
setattr(sys.modules[__name__], "PRESET_7D", PRESET_RECENT_WEEK)
PRESET_ALL = constants.PRESET_ALL
PRESET_CUSTOM = constants.PRESET_CUSTOM
PRESET_OPTIONS = constants.PRESET_OPTIONS
PRESET_LABELS = constants.PRESET_LABELS
PRESET_INLINE_LABELS = constants.PRESET_INLINE_LABELS
PRESET_DAYS = constants.PRESET_DAYS
DEFAULT_PRESET = constants.DEFAULT_PRESET
TZ_OFFSET_OPTIONS = constants.TZ_OFFSET_OPTIONS
DEFAULT_TZ_OFFSET_HOURS = constants.DEFAULT_TZ_OFFSET_HOURS
PARALLEL_READS_ENV = constants.PARALLEL_READS_ENV
PARALLEL_READS_MAX_WORKERS = constants.PARALLEL_READS_MAX_WORKERS
_TRUTHY = constants.TRUTHY
UNCONFIGURED_DB_MESSAGE = constants.UNCONFIGURED_DB_MESSAGE
_parse_parallel_reads_flag = read_mode.parse_parallel_reads_flag
DASHBOARD_PARALLEL_READS = _parse_parallel_reads_flag()
DateWindow = windows.DateWindow
default_date_range = windows.default_date_range
to_window = windows.to_window
_extent_dates = windows.extent_dates
preset_window = windows.preset_window
previous_window = windows.previous_window
format_tz_offset = filter_state.format_tz_offset
shift_ts = filter_state.shift_ts
parse_issue_number = filter_state.parse_issue_number
resolve_stage_filter = filter_state.resolve_stage_filter
DashboardCacheKey = filter_state.DashboardCacheKey
cache_key = filter_state.cache_key
db_unconfigured_message = read_mode.db_unconfigured_message
dashboard_parallel_reads_enabled = read_mode.dashboard_parallel_reads_enabled
_fan_out_reads = read_mode.fan_out_reads
