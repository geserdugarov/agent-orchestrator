# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Lazy-export inventory for the trajectory-viewer facade."""
from __future__ import annotations

from orchestrator._compat_exports import export_group


def _identity_exports(*names: str) -> tuple[tuple[str, str], ...]:
    return tuple(zip(names, names))


EXPORTS = (
    *export_group("orchestrator.dashboard_state", (("dashboard_state", None),)),
    *export_group("orchestrator.dashboard_theme", (("theme", None),)),
    *export_group("orchestrator.trajectory_reader", (("trajectory_reader", None),)),
    *export_group(
        "orchestrator.trajectory_reader",
        (("TrajectoryRun", "TrajectoryRun"),),
    ),
    *export_group(
        "orchestrator._trajectory_dashboard_html",
        _identity_exports(
            "EXTRA_CSS",
            "_REPO_LABEL",
            "_card_header_html",
            "_kpi_strip_html",
            "_labeled_chips_html",
            "_meta_html",
            "_run_picker_label",
            "_run_usage_html",
            "_runs_table_html",
            "_timeline_entry_html",
            "_timeline_with_usage",
            "_topbar_html",
            "_turn_usage_html",
        ),
    ),
    *export_group(
        "orchestrator._trajectory_dashboard_run_render",
        _identity_exports(
            "_render_run_notices",
            "_render_run_usage_and_chips",
            "_render_system_prompt",
            "_render_timeline_entry",
            "_render_timeline",
            "_render_run_card",
            "_render_run",
        ),
    ),
    *export_group(
        "orchestrator._trajectory_dashboard_models",
        _identity_exports("_TrajectoryFilters", "_TrajectoryPage"),
    ),
    *export_group(
        "orchestrator._trajectory_dashboard_page",
        _identity_exports(
            "NO_TRAJECTORIES_MESSAGE",
            "EMPTY_FILTER_MESSAGE",
            "_configure_page",
            "_stop_if_unconfigured",
            "_load_trajectory_page",
        ),
    ),
    *export_group(
        "orchestrator._trajectory_dashboard_filters",
        _identity_exports(
            "_render_categorical_filters",
            "_render_text_filters",
            "_render_trajectory_sidebar",
            "_filter_page_runs",
        ),
    ),
    *export_group(
        "orchestrator._trajectory_dashboard_picker",
        _identity_exports(
            "RUN_TABLE_LIMIT",
            "_render_no_trajectories",
            "_fixture_caption",
            "_render_run_list",
            "_pick_repo",
            "_pick_issue",
            "_pick_run",
            "_render_run_picker",
        ),
    ),
    *export_group(
        "orchestrator._trajectory_dashboard_runtime",
        _identity_exports(
            "_render_trajectory_footer",
            "_render_trajectory_page",
            "main",
        ),
    ),
)
