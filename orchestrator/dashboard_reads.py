# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Read orchestration for the analytics dashboard.

The Streamlit page in `orchestrator.dashboard` leans on this module for
everything between "resolved filters" and "read-model rows in hand":

- Filter-to-query adapters (`_filter_list`, `_read_filter_kwargs`,
  `_scoped_read`, `_read_filtered`) that turn a hashable cache-key tuple
  into read-model kwargs and run each read on the per-thread analytics
  connection.
- The cached reader wrappers -- the static-metadata reads
  (`_read_data_extent`, `_read_filter_options`) and the per-filter widget
  reads (`_read_summary` ... `_read_skill_adoption`) -- each of
  which stays connection-free so a raw `psycopg.Connection` never lands
  in the `st.cache_data` key.
- The reader registries (`_widget_task`, `_first_wave_readers`,
  `_second_wave_readers`, `_widget_readers`, `_build_read_keys`) that
  stage the widget reads into the two progressive-render waves and build
  the current + previous-window cache keys.
- The parallel dispatch (`_dispatch_reads`) that fans one wave out
  through `orchestrator.dashboard_state._fan_out_reads`, and the static-
  metadata data load (`_read_static_metadata`).
- The single `dashboard.load:` timing line (`_log_dashboard_load`).

Cache keys and TTLs, read ordering across the two waves, the
`DASHBOARD_PARALLEL_READS` toggle (resolved by the caller and handed in
as `parallel`), and the `AnalyticsReadError` -> one-banner-and-stop
behavior all live here so `orchestrator.dashboard` stays the Streamlit
render layer.

Streamlit is never imported: every helper that needs `st.cache_data`,
`st.spinner`, `st.error`, or `st.stop` takes the module object as a
plain `st` parameter. Together with the stdlib-plus-`orchestrator`
imports below (`analytics.read`, the import-light `dashboard_state` /
`dashboard_kpis` helpers), this keeps the module off the polling tick's
dependency footprint; `tests/test_dashboard.py` asserts the invariant.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from time import perf_counter
from typing import Any, Callable, Optional, Sequence

from orchestrator.analytics import read as analytics_read
from orchestrator.dashboard_kpis import DEFAULT_EXPENSIVE_LIMIT
from orchestrator.dashboard_state import (
    DateWindow,
    _fan_out_reads,
    cache_key,
    previous_window,
)

log = logging.getLogger(__name__)

DEFAULT_RECENT_AGENT_EXITS = 100

# TTL for the data-extent / filter-option reads (`get_data_extent`,
# `get_filter_options`). These reads carry no filter inputs and
# change only as `analytics.sync` ingests fresh events, so they
# tolerate a longer TTL than the 60 s window the per-filter cached
# wrappers use. Five minutes keeps a freshly-synced repo / event
# value reachable within one sync cycle while collapsing the
# topbar / sidebar round-trip on every rerun.
STATIC_METADATA_TTL_SECONDS = 300

LOADING_INDICATOR_MESSAGE = "Loading analytics…"

# One staged read: the widget name paired with the zero-argument callable
# `_fan_out_reads` dispatches. Named so the wave-reader and read-plan
# annotations stay shallow.
_ReaderTask = tuple[str, Callable[[], Any]]

# The name->data map one read wave returns. Named so the two-wave load's
# callback / return annotations stay shallow.
_ReadResults = dict[str, Any]


@dataclass(frozen=True)
class _DashboardReadPlan:
    first_wave: Sequence[_ReaderTask]
    second_wave: Sequence[_ReaderTask]
    parallel: bool
    started_at: float

    @property
    def total_reads(self) -> int:
        return len(self.first_wave) + len(self.second_wave)


def _filter_list(values_t: Optional[Sequence[str]]) -> Optional[list[str]]:
    """Convert a cached filter tuple back to the read model's list arg.

    `cache_key` stores the event / stage multiselects as hashable
    tuples so they can key `st.cache_data`; the `analytics.read`
    getters take lists. Converting per read keeps the tri-state intact
    -- `None` means "no filter", an empty selection means "show
    nothing", and the two must stay distinct at the read layer.
    """
    if values_t is None:
        return None
    return list(values_t)


def _scoped_read(getter: Callable[..., Any], /, **filters: Any) -> Any:
    """Run one windowed read on the per-thread analytics connection.

    Checks out the thread-local connection via `analytics_connection()`
    and forwards it to `getter` alongside the resolved filter kwargs, so
    every cached reader shares one open socket per render pass instead of
    opening (and hashing) a connection per call. The cached wrappers stay
    connection-free: `conn` is supplied here and never lands in their
    `st.cache_data` key (a raw `psycopg.Connection` is unhashable and
    would make every reload look like a cache miss).
    """
    with analytics_read.analytics_connection() as conn:
        return getter(conn=conn, **filters)


def _read_data_extent():
    return _scoped_read(analytics_read.get_data_extent)


def _read_filter_options():
    return _scoped_read(analytics_read.get_filter_options)


def _read_static_metadata(*, st: Any):
    """Read the data extent + filter options through cached wrappers.

    `get_data_extent` / `get_filter_options` carry no filter inputs (the
    cache key is empty) and only change as `analytics.sync` ingests new
    events, so both are cached under the longer `STATIC_METADATA_TTL_SECONDS`
    (5 min) rather than the per-filter 60 s TTL -- collapsing the sidebar /
    topbar round-trip on every rerun. Returns `(extent, options)`; a read
    error is surfaced as one `st.error` and stops the app.
    """
    read_data_extent = st.cache_data(
        show_spinner=False, ttl=STATIC_METADATA_TTL_SECONDS,
    )(_read_data_extent)
    read_filter_options = st.cache_data(
        show_spinner=False, ttl=STATIC_METADATA_TTL_SECONDS,
    )(_read_filter_options)

    try:
        return read_data_extent(), read_filter_options()
    except analytics_read.AnalyticsReadError as error:
        st.error(
            "Could not load analytics filter options: "
            f"{error}. Verify `ANALYTICS_DB_URL` and that the Postgres "
            "service is reachable, then reload."
        )
        st.stop()


def _read_filter_kwargs(key: tuple) -> dict[str, Any]:
    return {
        "start": key[0],
        "end": key[1],
        "repo": key[2],
        "events": _filter_list(key[3]),
        "stages": _filter_list(key[4]),
        "issue": key[5],
    }


def _read_filtered(
    getter: Callable[..., Any],
    key: tuple,
    **extra_filters: Any,
) -> Any:
    filters = _read_filter_kwargs(key)
    filters.update(extra_filters)
    return _scoped_read(getter, **filters)


def _read_summary(key: tuple):
    return _read_filtered(analytics_read.get_summary, key)


def _read_prev_kpi(key: tuple):
    # Previous-window read for the KPI delta pills and cost-trend
    # banner only. The full `get_summary` shape is never read off
    # `prev_summary`, so a thinner reader saves a `GROUP BY` follow-up
    # while leaving the cache key identical to `_read_summary`.
    return _read_filtered(analytics_read.get_kpi_prev, key)


def _read_time_series(key: tuple):
    return _read_filtered(analytics_read.get_time_series, key)


def _read_stage_breakdown(key: tuple):
    return _read_filtered(analytics_read.get_stage_breakdown, key)


def _read_recent_agent_exits(key: tuple):
    return _read_filtered(
        analytics_read.get_recent_agent_exits,
        key,
        limit=DEFAULT_RECENT_AGENT_EXITS,
    )


def _read_top_cost_issues(key: tuple):
    # Ask the database for the top-cost issues directly. Reading the
    # latest N issues by `last_seen` and re-sorting in Python silently
    # drops older high-cost issues that fall outside the truncated set.
    return _read_filtered(
        analytics_read.get_issues,
        key,
        limit=DEFAULT_EXPENSIVE_LIMIT,
        sort_by=analytics_read.SORT_BY_COST,
    )


def _read_review_round(key: tuple):
    return _read_filtered(analytics_read.get_review_round_breakdown, key)


def _read_backend_efficiency(key: tuple):
    return _read_filtered(analytics_read.get_backend_efficiency, key)


def _read_repo_breakdown(key: tuple):
    return _read_filtered(analytics_read.get_repo_breakdown, key)


def _read_cost_coverage(key: tuple):
    return _read_filtered(analytics_read.get_cost_coverage, key)


def _read_hourly_heatmap(
    key: tuple,
    tz_offset_hours: int,
):
    return _read_filtered(
        analytics_read.get_hourly_heatmap,
        key,
        tz_offset_hours=tz_offset_hours,
    )


def _read_throughput(key: tuple):
    return _read_filtered(analytics_read.get_throughput_breakdown, key)


def _read_backend_daily_tokens(key: tuple):
    return _read_filtered(analytics_read.get_backend_daily_tokens, key)


def _read_skill_trigger_rates(key: tuple):
    return _read_filtered(analytics_read.get_skill_trigger_rates, key)


def _read_skill_trigger_matrix(key: tuple):
    return _read_filtered(analytics_read.get_skill_trigger_matrix, key)


def _read_skill_adoption(key: tuple):
    return _read_filtered(analytics_read.get_skill_adoption, key)


def _widget_task(
    st: Any,
    name: str,
    reader: Callable[..., Any],
    *args: Any,
) -> _ReaderTask:
    cached_reader = st.cache_data(show_spinner=False, ttl=60)(reader)
    return name, partial(cached_reader, *args)


def _first_wave_readers(
    st: Any,
    key: tuple,
    prev_key: tuple,
) -> list[_ReaderTask]:
    return [
        _widget_task(st, "summary", _read_summary, key),
        _widget_task(st, "prev_summary", _read_prev_kpi, prev_key),
        _widget_task(st, "ts_points", _read_time_series, key),
        _widget_task(st, "review_round_rows", _read_review_round, key),
        _widget_task(st, "throughput_rows", _read_throughput, key),
        _widget_task(st, "cost_coverage_rows", _read_cost_coverage, key),
    ]


def _second_wave_readers(
    st: Any,
    key: tuple,
    tz_offset_choice: int,
) -> list[_ReaderTask]:
    return [
        _widget_task(st, "stage_rows", _read_stage_breakdown, key),
        _widget_task(st, "agent_exits", _read_recent_agent_exits, key),
        _widget_task(st, "issues_rows", _read_top_cost_issues, key),
        _widget_task(st, "backend_rows", _read_backend_efficiency, key),
        _widget_task(st, "repo_rows", _read_repo_breakdown, key),
        _widget_task(
            st,
            "heatmap_rows",
            _read_hourly_heatmap,
            key,
            int(tz_offset_choice),
        ),
        _widget_task(st, "backend_daily_rows", _read_backend_daily_tokens, key),
        _widget_task(st, "skill_adoption_rows", _read_skill_adoption, key),
        _widget_task(st, "skill_rows", _read_skill_trigger_rates, key),
        _widget_task(st, "skill_matrix_rows", _read_skill_trigger_matrix, key),
    ]


def _widget_readers(*, st: Any, key, prev_key, tz_offset_choice: int):
    """Define the cached per-filter read wrappers and stage them.

    Returns `(first_wave_readers, second_wave_readers)` -- each a list of
    `(name, zero-arg callable)` pairs `_fan_out_reads` dispatches.

    Connection scoping: each wrapper delegates through `_read_filtered`
    to `_scoped_read`, which checks out the thread-local connection via
    `analytics_connection()` and forwards it to the read helper rather
    than threading a connection through the cache key (a raw
    `psycopg.Connection` is not hashable and would crash the wrapper, and
    every reload would otherwise look like a cache miss). The thread-local
    persists across reads in the same render pass, so the first cache-miss
    pays the psycopg handshake and the rest reuse the open socket. The
    cache key stays the filter tuple `(start, end, repo, events_t,
    stages_t, issue)`.

    Split into two staged waves so the topbar / filter meta / insight
    banners / KPI strip can paint as soon as their inputs are available
    instead of blocking on every widget: the first wave carries the six
    reads those above-the-fold widgets consume, the second the ten
    remaining widget reads. Each task is a zero-argument `partial` bound
    to its immutable filter tuple, and worker threads only return data --
    every `st.*` write happens on the caller's render thread between waves.
    """
    return (
        _first_wave_readers(st, key, prev_key),
        _second_wave_readers(st, key, tz_offset_choice),
    )


def _build_read_keys(
    *,
    window: DateWindow,
    repo_filter: Optional[str],
    event_filter: Optional[Sequence[str]],
    stage_filter: Optional[Sequence[str]],
    issue_filter: Optional[int],
):
    """Build the current + previous-window cache-key tuples.

    Returns `(key, prev_key)`: the `(start, end, repo, events, stages,
    issue)` tuple the fan-out reads are cached under, and the same
    tuple shifted to the immediately-preceding equal-length window for
    the KPI delta pills. Cached readers accept the tuple as one hashable
    key and `_read_filter_kwargs` expands its stable field order for the
    read model.
    """
    key = cache_key(
        window, repo_filter, event_filter, stage_filter, issue_filter
    )
    prev_key = cache_key(
        previous_window(window),
        repo_filter,
        event_filter,
        stage_filter,
        issue_filter,
    )
    return key, prev_key


def _dispatch_reads(readers, *, st: Any, parallel: bool):
    """Dispatch one read wave and surface a read error as one banner.

    Runs the wave through `_fan_out_reads` (sequential, or across a thread
    pool when `parallel`) and returns the name->data dict. An
    `AnalyticsReadError` from any reader is caught, rendered as one
    `st.error`, and stops the app -- the dashboard cannot render without
    database access.
    """
    try:
        return _fan_out_reads(readers, parallel=parallel)
    except analytics_read.AnalyticsReadError as error:
        st.error(
            f"Analytics query failed: {error}. The dashboard cannot render "
            "without database access; check Postgres connectivity and "
            "reload."
        )
        st.stop()


def _log_dashboard_load(*, load_start: float, reads: int, parallel: bool) -> None:
    """Emit the single `dashboard.load:` INFO line for the A/B rollout.

    Carries total wall-clock, the reader count (6 when the empty-window
    short-circuit skips the second wave, else 16), and the parallel flag,
    so the sequential / parallel paths can be A/B'd with one
    `grep dashboard.load streamlit.log`.
    """
    log.info(
        "dashboard.load: total=%.1fs reads=%d parallel=%s",
        perf_counter() - load_start,
        reads,
        "true" if parallel else "false",
    )


def _run_read_waves(
    reads: _DashboardReadPlan,
    *,
    st: Any,
    render_first_wave: Callable[[_ReadResults], Any],
) -> Optional[tuple[_ReadResults, Any]]:
    """Dispatch the two staged read waves in order and merge their data.

    Runs the first wave under the loading spinner, then hands its results
    to `render_first_wave` on the caller's render thread (it paints the
    topbar / KPI strip and returns the first-wave render state, or `None`
    to short-circuit on an empty window). Only when the first wave produced
    data does the second wave dispatch and merge in. Emits the single
    `dashboard.load:` timing line once both waves land. Returns
    `(read_results, first_wave)`, or `None` when the empty-window branch
    skips the second wave -- that branch logs its own truncated load line,
    so the full log below is not reached.
    """
    with st.spinner(LOADING_INDICATOR_MESSAGE):
        read_results = _dispatch_reads(
            reads.first_wave,
            st=st,
            parallel=reads.parallel,
        )
        first_wave = render_first_wave(read_results)
        if first_wave is None:
            return None
        read_results.update(_dispatch_reads(
            reads.second_wave,
            st=st,
            parallel=reads.parallel,
        ))
    _log_dashboard_load(
        load_start=reads.started_at,
        reads=reads.total_reads,
        parallel=reads.parallel,
    )
    return read_results, first_wave
