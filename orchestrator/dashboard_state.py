# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Date / filter / session-state helpers for the analytics dashboard.

Pure logic the Streamlit page in `orchestrator.dashboard` leans on to
turn the sidebar / filter-bar inputs into read-model arguments: the
inclusive-window math (`DateWindow`, `to_window`, `preset_window`,
`previous_window`), the preset / timezone vocabulary, the stage-filter
and cache-key resolution, the issue-number parser, the DB-config
banner check, and the read fan-out switch (`DASHBOARD_PARALLEL_READS`,
`_fan_out_reads`). Everything here is import-light -- only stdlib plus
`orchestrator.analytics` -- so importing it never pulls Streamlit,
pandas, or Plotly into the polling tick's import surface.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

from orchestrator import analytics
from orchestrator.analytics.read import DataExtent

DEFAULT_WINDOW_DAYS = 7

# Sidebar window presets. `Custom` keeps the two-date picker so the
# operator can pin an arbitrary window inside the data extent. The
# redesigned topbar exposes the presets inline (`3D` / `7D` / `All`)
# matching the standalone mock; `Custom` stays available in the
# sidebar so the existing per-issue drill-down workflow still works.
PRESET_3D = "3d"
PRESET_7D = "7d"
PRESET_ALL = "All"
PRESET_CUSTOM = "Custom"
PRESET_OPTIONS: tuple[str, ...] = (PRESET_3D, PRESET_7D, PRESET_ALL, PRESET_CUSTOM)
PRESET_LABELS: dict[str, str] = {
    PRESET_3D: "Last 3 days",
    PRESET_7D: "Last 7 days",
    PRESET_ALL: "All time",
    PRESET_CUSTOM: "Custom range",
}
PRESET_INLINE_LABELS: dict[str, str] = {
    PRESET_3D: "3D",
    PRESET_7D: "7D",
    PRESET_ALL: "All",
}
PRESET_DAYS: dict[str, int] = {PRESET_3D: 3, PRESET_7D: 7}
DEFAULT_PRESET = PRESET_ALL

# UTC-offset selector for the "When agents run" heatmap and the
# "Recent agent runs" table. `ts` is stored in UTC; the dashboard
# applies the offset at read time (heatmap bucketing) and at
# display time (recent-runs table). Range -12 .. +14 covers every
# IANA-style fixed offset in use; default `+7` matches the
# operator's home timezone.
TZ_OFFSET_OPTIONS: tuple[int, ...] = tuple(range(-12, 15))
DEFAULT_TZ_OFFSET_HOURS = 7

# Parallel read fan-out for `main()`'s 14 independent widget reads.
# Opt-in via `DASHBOARD_PARALLEL_READS` so the new path can be A/B'd
# against the sequential baseline. Default off: the reads keep running
# one-at-a-time on the Streamlit render thread unless the operator
# flips this on. Truthy spellings follow the same vocabulary as other
# boolean knobs in the codebase (`DECOMPOSE` etc.). Parsed at module
# import like `ANALYTICS_DB_URL`, so a Streamlit restart picks up the
# change without per-render env reads.
PARALLEL_READS_ENV = "DASHBOARD_PARALLEL_READS"
PARALLEL_READS_MAX_WORKERS = 8
_TRUTHY = frozenset({"1", "true", "on", "yes"})


def _parse_parallel_reads_flag() -> bool:
    raw = os.environ.get(PARALLEL_READS_ENV, "").strip().lower()
    return raw in _TRUTHY


DASHBOARD_PARALLEL_READS: bool = _parse_parallel_reads_flag()

UNCONFIGURED_DB_MESSAGE = (
    "`ANALYTICS_DB_URL` is not configured. Set it in your environment "
    "(see `.env.example.advanced` and `docs/configuration.md`) and "
    "reload the dashboard to view analytics."
)


@dataclass(frozen=True)
class DateWindow:
    """Inclusive-start, exclusive-end datetime window."""

    start: datetime
    end: datetime


def default_date_range(
    *,
    today: Optional[date] = None,
    days: int = DEFAULT_WINDOW_DAYS,
) -> tuple[date, date]:
    """Default `[start, end]` inclusive date range for the sidebar.

    Kept for callers and tests that pre-dated the data-extent-bounded
    preset selector. `today` injection keeps this testable; the
    production path relies on `date.today()`. `days` is clamped at 1
    so `days=0` (an explicit "today only" choice) still returns
    `(today, today)` rather than a reversed range.
    """
    end = today or date.today()
    start = end - timedelta(days=max(days - 1, 0))
    return start, end


def to_window(start_date: date, end_date: date) -> DateWindow:
    """Convert inclusive `[start_date, end_date]` to a `DateWindow`."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(
        end_date + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    return DateWindow(start=start_dt, end=end_dt)


def _extent_dates(extent: DataExtent) -> Optional[tuple[date, date]]:
    """`(min_date, max_date)` from a data extent, or `None` when empty."""
    if extent.min_ts is None or extent.max_ts is None:
        return None
    return extent.min_ts.date(), extent.max_ts.date()


def preset_window(
    preset: str, extent: DataExtent
) -> Optional[DateWindow]:
    """Resolve a sidebar preset into a data-extent-bounded `DateWindow`.

    The presets are anchored at the data extent's max date (not
    today) so a freshly-deployed Postgres whose latest event is days
    old still surfaces the last days of recorded activity. Returns
    `None` when the extent is empty (no events yet) or `preset` is
    `Custom` (the caller renders a date-range picker instead). An
    unknown preset string also returns `None`.

    For `3d` / `7d`, the start date is clamped to
    `max(extent.min_date, max_date - (n - 1))` so short data histories
    do not produce windows reaching before the first recorded event.
    """
    bounds = _extent_dates(extent)
    if bounds is None:
        return None
    min_d, max_d = bounds
    if preset == PRESET_ALL:
        return to_window(min_d, max_d)
    days = PRESET_DAYS.get(preset)
    if days is None:
        return None
    start_d = max(max_d - timedelta(days=days - 1), min_d)
    return to_window(start_d, max_d)


def previous_window(window: DateWindow) -> DateWindow:
    """Window of the same length ending at `window.start`."""
    length = window.end - window.start
    return DateWindow(start=window.start - length, end=window.start)


def format_tz_offset(hours: int) -> str:
    """Render an integer UTC offset as `UTC` / `UTC+N` / `UTC-N`."""
    if hours == 0:
        return "UTC"
    sign = "+" if hours > 0 else "-"
    return f"UTC{sign}{abs(int(hours))}"


def shift_ts(ts: Any, offset: timedelta) -> Any:
    """Return `ts` shifted by `offset` so the displayed wall-clock
    reflects the selected UTC offset; `None` passes through.

    The orchestrator persists `ts` in UTC; the dashboard adds the
    operator's selected offset before display so the "Recent agent
    runs" table reads in the same timezone the heatmap was bucketed
    in. Naive datetimes (no tzinfo) are shifted in place; aware
    datetimes are converted via `astimezone(timezone(offset))` so the
    rendered string still shows the wall-clock for the selected
    offset rather than the original UTC reading.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts + offset
        return ts.astimezone(timezone(offset))
    return ts


def parse_issue_number(raw: str) -> Optional[int]:
    """Lenient `#123` / `123` parser for the drill-down input."""
    if not raw:
        return None
    s = raw.strip().lstrip("#").strip()
    if not s:
        return None
    try:
        n = int(s)
    except ValueError:
        return None
    return n if n > 0 else None


def db_unconfigured_message() -> Optional[str]:
    """Single source of truth for the "no DB configured" banner."""
    if not analytics.ANALYTICS_DB_URL:
        return UNCONFIGURED_DB_MESSAGE
    return None


def dashboard_parallel_reads_enabled() -> bool:
    """True when `DASHBOARD_PARALLEL_READS` is set to a truthy sentinel.

    Default OFF -- the parallel fan-out is opt-in so operators can A/B
    against the sequential baseline before flipping it on. Truthy
    values: `1` / `true` / `on` / `yes` (case-insensitive). Anything
    else (including empty / unset) keeps the sequential path. Reads
    the module-level `DASHBOARD_PARALLEL_READS` so tests can patch the
    attribute directly (mirrors how `analytics.ANALYTICS_DB_URL` is
    exposed).
    """
    return DASHBOARD_PARALLEL_READS


def _fan_out_reads(
    readers: Sequence[tuple[str, Callable[[], Any]]],
    *,
    parallel: bool,
    max_workers: int = PARALLEL_READS_MAX_WORKERS,
) -> dict[str, Any]:
    """Run each `(name, callable)` reader and return `{name: result}`.

    `parallel=False` runs readers one-at-a-time in submission order on
    the calling thread -- the sequential baseline. The thread-local
    `analytics_connection` keeps the single psycopg socket warm across
    all 14 reads.

    `parallel=True` dispatches across a `ThreadPoolExecutor` capped at
    `max_workers`. Each worker thread opens its own thread-local
    analytics connection on first use and reuses it across whatever
    subset of the readers lands on it -- `psycopg.Connection` is not
    thread-safe for concurrent use, so sharing one socket across
    workers would corrupt the wire protocol; the per-thread cache in
    `analytics.read.analytics_connection` keeps each worker's socket
    isolated. The wall-clock collapses to roughly the slowest reader
    in a wave of `max_workers` plus a small executor overhead.

    Any exception raised by a reader propagates verbatim from the
    first failing future (matching the sequential path's "stop at the
    first error" shape) so the caller can surface a single
    user-friendly `AnalyticsReadError` message. Results are returned
    in `readers` submission order so the call sites can unpack them
    without caring about completion order.
    """
    if not parallel:
        return {name: fn() for name, fn in readers}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [(name, pool.submit(fn)) for name, fn in readers]
        return {name: fut.result() for name, fut in futures}


def resolve_stage_filter(
    selected: Sequence[str],
    available: Sequence[str],
) -> Optional[list[str]]:
    """Resolve the sidebar stage multiselect into a read-model filter.

    See module docstring of `orchestrator.analytics.read` for the
    tri-state contract (`None` = no filter, `[]` = FALSE, non-empty
    = `IN (...)`). The default "all selected" must collapse to `None`
    so NULL-stage rows are not silently excluded.
    """
    if not available:
        return None
    if set(selected) == set(available):
        return None
    return list(selected)


def cache_key(
    window: DateWindow,
    repo: Optional[str],
    events: Optional[Sequence[str]],
    stages: Optional[Sequence[str]],
    issue: Optional[int],
) -> tuple:
    """Hashable cache key for the dashboard's window-scoped reads."""
    events_t = tuple(events) if events is not None else None
    stages_t = tuple(stages) if stages is not None else None
    return (window.start, window.end, repo, events_t, stages_t, issue)
