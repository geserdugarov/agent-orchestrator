# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory-viewer sidebar filters and run selection."""
from __future__ import annotations

from typing import Any, Sequence

from orchestrator import dashboard_state, trajectory_reader
from orchestrator import _trajectory_dashboard_models as models
from orchestrator._trajectory_dashboard_html import _REPO_LABEL


def _render_categorical_filters(
    st: Any,
    options: trajectory_reader.FilterOptions,
) -> tuple[Sequence[str], Sequence[str], Sequence[str]]:
    backends = st.multiselect(
        "Backend",
        list(options.backends),
        help="Leave empty to include every backend.",
    )
    roles = st.multiselect(
        "Agent role",
        list(options.agent_roles),
        help="Leave empty to include every role.",
    )
    stages = st.multiselect(
        "Stage",
        list(options.stages),
        help="Leave empty to include every stage.",
    )
    return backends, roles, stages


def _render_text_filters(st: Any) -> tuple[str, str]:
    issue_input = st.text_input(
        "Issue number",
        value="",
        help="Enter `123` or `#123` to narrow to one issue.",
    )
    query_input = st.text_input(
        "Search",
        value="",
        help=(
            "Case-insensitive substring matched across the prompt, "
            "system prompt, output, tool names, tool payloads, and skill names."
        ),
    )
    return issue_input, query_input


def _render_trajectory_sidebar(
    st: Any,
    options: trajectory_reader.FilterOptions,
) -> models._TrajectoryFilters:
    with st.sidebar:
        st.header("Filters")
        repo_choice = st.selectbox(_REPO_LABEL, ("All", *options.repos), index=0)
        categorical = _render_categorical_filters(st, options)
        text_filters = _render_text_filters(st)
        hide_fixtures = st.checkbox(
            "Hide synthetic fixtures",
            value=False,
            help=(
                "Drop records that look like test-suite fixtures -- a "
                "sentinel `ignored` prompt, a `sess-*` session id, or a "
                "Skill-only run. Leave off to keep them, flagged with a "
                "`fixture` tag in the table and run picker."
            ),
        )
    return models._TrajectoryFilters(
        repo=None if repo_choice == "All" else repo_choice,
        backends=categorical[0] or None,
        agent_roles=categorical[1] or None,
        stages=categorical[2] or None,
        issue=dashboard_state.parse_issue_number(text_filters[0]),
        query=text_filters[1],
        hide_fixtures=hide_fixtures,
    )


def _filter_page_runs(
    page: models._TrajectoryPage,
    filters: models._TrajectoryFilters,
) -> list[trajectory_reader.TrajectoryRun]:
    return trajectory_reader.filter_runs(
        page.runs,
        repo=filters.repo,
        backends=filters.backends,
        agent_roles=filters.agent_roles,
        stages=filters.stages,
        issue=filters.issue,
        query=filters.query,
        exclude_fixtures=filters.hide_fixtures,
    )
