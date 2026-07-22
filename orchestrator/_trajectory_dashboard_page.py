# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory-viewer setup and local-file loading."""
from __future__ import annotations

from typing import Any

from orchestrator import dashboard_theme as theme
from orchestrator import trajectory_reader
from orchestrator import _trajectory_dashboard_models as models
from orchestrator._trajectory_dashboard_html import EXTRA_CSS, _topbar_html


NO_TRAJECTORIES_MESSAGE = (
    "No `agent_trajectory` records were found. The trajectory sink writes "
    "one record per tracked agent run once `TRAJECTORY_LOG_PATH` is set and "
    "the orchestrator has run at least one agent. Confirm the path below and "
    "that some workflow activity has happened since the sink was enabled."
)
EMPTY_FILTER_MESSAGE = (
    "No trajectories match the current filters. Clear a filter or broaden "
    "the search to see recorded runs."
)


def _configure_page(st: Any) -> None:
    st.set_page_config(page_title="Orchestrator Trajectories", layout="wide")
    st.markdown(theme.PAGE_CSS, unsafe_allow_html=True)
    st.markdown(EXTRA_CSS, unsafe_allow_html=True)


def _stop_if_unconfigured(st: Any) -> None:
    message = trajectory_reader.log_unconfigured_message()
    if not message:
        return
    st.markdown(_topbar_html(0, 0), unsafe_allow_html=True)
    st.warning(message)
    st.stop()


def _load_trajectory_page() -> models._TrajectoryPage:
    log_path = trajectory_reader.resolve_log_path()
    runs = trajectory_reader.read_trajectories()
    return models._TrajectoryPage(
        log_path=log_path,
        runs=runs,
        options=trajectory_reader.filter_options(runs),
        fixture_total=sum(1 for run in runs if run.is_fixture),
    )
