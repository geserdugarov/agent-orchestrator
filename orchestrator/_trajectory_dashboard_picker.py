# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory overview table and cascading run picker."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from orchestrator._trajectory_dashboard_html import (
    _REPO_LABEL,
    _run_picker_label,
    _runs_table_html,
)
from orchestrator._trajectory_dashboard_page import NO_TRAJECTORIES_MESSAGE
from orchestrator._trajectory_dashboard_run_render import _render_run
from orchestrator.trajectory_reader import TrajectoryRun


RUN_TABLE_LIMIT = 200


def _render_no_trajectories(st: Any, log_path: Optional[Path]) -> None:
    st.info(NO_TRAJECTORIES_MESSAGE)
    if log_path is not None:
        st.caption(f"Reading `{log_path}`.")


def _fixture_caption(fixture_total: int, hide_fixtures: bool) -> str:
    noun = "run" if fixture_total == 1 else "runs"
    if hide_fixtures:
        return f"{fixture_total} synthetic fixture {noun} hidden."
    return (
        f"{fixture_total} synthetic fixture {noun} flagged; "
        "tick *Hide synthetic fixtures* in the sidebar to drop them."
    )


def _render_run_list(
    st: Any,
    shown: Sequence[TrajectoryRun],
    fixture_total: int,
    hide_fixtures: bool,
) -> None:
    with st.expander("Recorded runs", expanded=True):
        st.caption("Most recent first · pick a run below to inspect it")
        st.markdown(
            _runs_table_html(shown[:RUN_TABLE_LIMIT]),
            unsafe_allow_html=True,
        )
        if len(shown) > RUN_TABLE_LIMIT:
            st.caption(
                f"Table shows the {RUN_TABLE_LIMIT} most recent of "
                f"{len(shown)} matching runs; the picker below lists all of "
                "them. Narrow the filters to shorten the list."
            )
        if fixture_total:
            st.caption(_fixture_caption(fixture_total, hide_fixtures))


def _pick_repo(st: Any, shown: Sequence[TrajectoryRun]) -> str:
    repos = sorted({run.repo for run in shown})
    return st.selectbox(_REPO_LABEL, repos)


def _pick_issue(
    st: Any,
    shown: Sequence[TrajectoryRun],
    repo: str,
) -> int:
    issues = sorted({run.issue for run in shown if run.repo == repo})
    return st.selectbox("Issue", issues, format_func=lambda issue: f"#{issue}")


def _pick_run(
    st: Any,
    shown: Sequence[TrajectoryRun],
    repo: str,
    issue: int,
) -> TrajectoryRun:
    candidates = [
        run
        for run in shown
        if run.repo == repo and run.issue == issue
    ]
    selected = st.selectbox(
        "Run",
        range(len(candidates)),
        format_func=lambda index: _run_picker_label(candidates[index]),
    )
    return candidates[selected]


def _render_run_picker(st: Any, shown: Sequence[TrajectoryRun]) -> None:
    st.markdown(
        '<p class="orch-card-sub" style="margin:14px 0 4px">Inspect run</p>',
        unsafe_allow_html=True,
    )
    columns = st.columns(3)
    with columns[0]:
        repo = _pick_repo(st, shown)
    with columns[1]:
        issue = _pick_issue(st, shown, repo)
    with columns[2]:
        run = _pick_run(st, shown, repo, issue)
    _render_run(st=st, run=run)
