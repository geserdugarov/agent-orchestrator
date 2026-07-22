# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard staged read plan and reader registries."""
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Optional, Sequence

from orchestrator._dashboard_read_breakdowns import (
    _read_backend_daily_tokens,
    _read_backend_efficiency,
    _read_cost_coverage,
    _read_hourly_heatmap,
    _read_repo_breakdown,
    _read_skill_trigger_rates,
    _read_throughput,
)
from orchestrator._dashboard_read_rollups import (
    _read_prev_kpi,
    _read_recent_agent_exits,
    _read_review_round,
    _read_stage_breakdown,
    _read_summary,
    _read_time_series,
    _read_top_cost_issues,
)
from orchestrator._dashboard_read_skills import (
    _read_skill_adoption,
    _read_skill_trigger_matrix,
)
from orchestrator.dashboard_state import DateWindow, cache_key, previous_window


_ReaderTask = tuple[str, Callable[[], Any]]


@dataclass(frozen=True)
class _DashboardReadPlan:
    first_wave: Sequence[_ReaderTask]
    second_wave: Sequence[_ReaderTask]
    parallel: bool
    started_at: float

    @property
    def total_reads(self) -> int:
        return len(self.first_wave) + len(self.second_wave)


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
    """Return the first and second cached read waves."""
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
    """Build current and previous-window cache keys."""
    key = cache_key(
        window,
        repo_filter,
        event_filter,
        stage_filter,
        issue_filter,
    )
    prev_key = cache_key(
        previous_window(window),
        repo_filter,
        event_filter,
        stage_filter,
        issue_filter,
    )
    return key, prev_key
