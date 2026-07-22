# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Recent-run table and per-issue drill-down widgets."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from orchestrator.analytics import read as analytics_read
from orchestrator import _dashboard_widget_models as models
from orchestrator.dashboard_reads import _filter_list, _scoped_read
from orchestrator.dashboard_state import shift_ts


NO_AGENT_EXITS_MESSAGE = "No `agent_exit` rows match the current filters."


def _render_recent_runs(
    *,
    st: Any,
    pd: Any,
    agent_exits: Any,
    tz_offset_choice: int,
) -> None:
    """Render recent agent runs in the selected timezone."""
    with st.expander("Recent agent runs", expanded=False):
        if agent_exits:
            timestamp_offset = timedelta(hours=int(tz_offset_choice))
            exit_frame = pd.DataFrame(
                [
                    {
                        "ts": shift_ts(exit_row.ts, timestamp_offset),
                        "repo": exit_row.repo,
                        "issue": exit_row.issue,
                        "stage": exit_row.stage,
                        "agent": exit_row.agent_role,
                        "backend": exit_row.backend,
                        "duration (s)": exit_row.duration_s,
                        "exit": exit_row.exit_code,
                        "timed out": exit_row.timed_out,
                        "round": exit_row.review_round,
                        "retry": exit_row.retry_count,
                        "input tokens": exit_row.input_tokens,
                        "output tokens": exit_row.output_tokens,
                        "cost (USD)": exit_row.cost_usd,
                        "cost source": exit_row.cost_source,
                    }
                    for exit_row in agent_exits
                ]
            )
            st.dataframe(exit_frame, use_container_width=True)
        else:
            st.info(NO_AGENT_EXITS_MESSAGE)


def _render_drilldown_view(
    modules: models._DashboardModules,
    filters: models._DashboardFilters,
) -> None:
    """Render the per-issue event trace when an issue is selected."""
    if filters.issue_input is None:
        return
    modules.st.subheader(f"Issue #{filters.issue_input} drill-down")
    if filters.repo is None:
        modules.st.info(
            "Pick a specific repo in the sidebar before drilling "
            "into an issue number -- GitHub issue numbers repeat across repos."
        )
        return
    try:
        trace = _scoped_read(
            analytics_read.get_issue_events,
            repo=filters.repo,
            issue=filters.issue_input,
            start=filters.window.start,
            end=filters.window.end,
            events=_filter_list(filters.events),
            stages=_filter_list(filters.stages),
        )
    except analytics_read.AnalyticsReadError as error:
        modules.st.error(f"Issue drill-down failed: {error}")
        return
    if trace:
        modules.st.dataframe(
            modules.pd.DataFrame(
                [
                    {
                        "ts": event.ts,
                        "event": event.event,
                        "stage": event.stage,
                        "duration (s)": event.duration_s,
                        "result": event.result,
                        "agent": event.agent_role,
                        "backend": event.backend,
                        "exit": event.exit_code,
                        "cost (USD)": event.cost_usd,
                    }
                    for event in trace
                ]
            ),
            use_container_width=True,
        )
    else:
        modules.st.info(
            f"No analytics events recorded for "
            f"`{filters.repo}#{filters.issue_input}` under the current filters."
        )
