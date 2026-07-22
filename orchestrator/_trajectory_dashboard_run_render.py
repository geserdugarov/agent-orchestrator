# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Selected trajectory-run detail rendering."""
from __future__ import annotations

from typing import Any, Optional

from orchestrator import trajectory_reader
from orchestrator._trajectory_dashboard_html import (
    _card_header_html,
    _labeled_chips_html,
    _meta_html,
    _run_usage_html,
    _timeline_entry_html,
    _timeline_with_usage,
    _turn_usage_html,
)


def _render_run_notices(st: Any, run: trajectory_reader.TrajectoryRun) -> None:
    if run.is_fixture:
        st.info(
            "This run is flagged as a likely synthetic test fixture "
            "(a sentinel `ignored` prompt, a `sess-*` session id, or a "
            "Skill-only run). Such records can appear in a trajectory "
            "file inherited from a run with the sink enabled during the "
            "test suite."
        )
    if run.truncated:
        st.warning(
            "This trajectory was truncated by the sink's record budget; "
            "later steps were dropped before the run finished."
        )


def _render_run_usage_and_chips(
    st: Any,
    run: trajectory_reader.TrajectoryRun,
) -> None:
    usage_html = _run_usage_html(run)
    if usage_html:
        st.markdown(usage_html, unsafe_allow_html=True)
    for label, names in (
        ("Tools offered", run.tools),
        ("Skills triggered", run.skills_triggered),
        ("Skills available", run.skills_available),
    ):
        chips = _labeled_chips_html(label, names)
        if chips:
            st.markdown(chips, unsafe_allow_html=True)


def _render_system_prompt(st: Any, run: trajectory_reader.TrajectoryRun) -> None:
    if not run.system_prompt:
        return
    with st.expander("System prompt", expanded=False):
        st.code(run.system_prompt)


def _render_timeline_entry(
    st: Any,
    index: int,
    strip: Optional[trajectory_reader.TurnUsageView],
    entry: trajectory_reader.TimelineEntry,
) -> None:
    if strip is not None:
        st.markdown(_turn_usage_html(strip), unsafe_allow_html=True)
    st.markdown(_timeline_entry_html(entry, index), unsafe_allow_html=True)
    if not entry.content:
        return
    if entry.is_output:
        st.markdown(entry.content)
    else:
        st.code(entry.content)


def _render_timeline(st: Any, run: trajectory_reader.TrajectoryRun) -> None:
    st.markdown(
        '<p class="orch-card-sub" style="margin-top:14px">'
        f"Trajectory timeline · {run.step_count} steps · "
        f"{run.tool_calls} tool calls</p>",
        unsafe_allow_html=True,
    )
    if not run.timeline:
        st.caption("No timeline entries were recorded for this run.")
        return
    for index, (strip, entry) in enumerate(_timeline_with_usage(run)):
        _render_timeline_entry(st, index, strip, entry)


def _render_run_card(st: Any, run: trajectory_reader.TrajectoryRun) -> None:
    st.markdown('<div class="orch-cardmark"></div>', unsafe_allow_html=True)
    repo_label = run.repo or "unknown repo"
    st.markdown(
        _card_header_html(
            f"Run #{run.issue} · {repo_label}",
            "Ordered timeline: prompt, text turns, tool calls, output",
        ),
        unsafe_allow_html=True,
    )
    _render_run_notices(st, run)
    st.markdown(_meta_html(run), unsafe_allow_html=True)
    _render_run_usage_and_chips(st, run)
    _render_system_prompt(st, run)
    _render_timeline(st, run)


def _render_run(*, st: Any, run: trajectory_reader.TrajectoryRun) -> None:
    """Render the detail card for one selected run."""
    with st.container(border=True):
        _render_run_card(st, run)
