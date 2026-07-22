# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard staged read dispatch and load timing."""
from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, Callable, Optional

from orchestrator.analytics import read as analytics_read
from orchestrator._dashboard_read_plan import _DashboardReadPlan
from orchestrator.dashboard_state import _fan_out_reads


LOADING_INDICATOR_MESSAGE = "Loading analytics…"
_ReadResults = dict[str, Any]
log = logging.getLogger(__name__)


def _dispatch_reads(readers, *, st: Any, parallel: bool):
    """Dispatch one read wave and surface a read error as one banner."""
    try:
        return _fan_out_reads(readers, parallel=parallel)
    except analytics_read.AnalyticsReadError as error:
        st.error(
            f"Analytics query failed: {error}. The dashboard cannot render "
            "without database access; check Postgres connectivity and reload."
        )
        st.stop()


def _log_dashboard_load(
    *,
    load_start: float,
    reads: int,
    parallel: bool,
) -> None:
    """Emit the dashboard load timing line."""
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
    """Dispatch both read waves and merge their data."""
    with st.spinner(LOADING_INDICATOR_MESSAGE):
        read_results = _dispatch_reads(
            reads.first_wave,
            st=st,
            parallel=reads.parallel,
        )
        first_wave = render_first_wave(read_results)
        if first_wave is None:
            return None
        read_results.update(
            _dispatch_reads(
                reads.second_wave,
                st=st,
                parallel=reads.parallel,
            )
        )
    _log_dashboard_load(
        load_start=reads.started_at,
        reads=reads.total_reads,
        parallel=reads.parallel,
    )
    return read_results, first_wave
