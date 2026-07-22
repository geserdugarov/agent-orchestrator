# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard facade bootstrap shared by package and direct launches."""
from __future__ import annotations

import importlib
import sys
from typing import Any, NamedTuple


_RELOADABLE_MODULES = (
    "orchestrator._dashboard_date_range",
    "orchestrator._dashboard_date_widgets",
    "orchestrator._dashboard_drilldown",
    "orchestrator._dashboard_filter_state",
    "orchestrator._dashboard_page_controls",
    "orchestrator._dashboard_read_mode",
    "orchestrator._dashboard_runtime",
    "orchestrator._dashboard_state_constants",
    "orchestrator._dashboard_windows",
    "orchestrator._dashboard_widget_costs",
    "orchestrator._dashboard_widget_models",
    "orchestrator._dashboard_widget_pipeline",
    "orchestrator._dashboard_widget_runs",
    "orchestrator._dashboard_widget_skills",
    "orchestrator._dashboard_widget_states",
    "orchestrator._dashboard_widget_usage",
    "orchestrator.dashboard_cards",
    "orchestrator.dashboard_html",
    "orchestrator.dashboard_kpi_strip",
    "orchestrator.dashboard_kpis",
    "orchestrator.dashboard_reads",
    "orchestrator.dashboard_skill_adoption",
    "orchestrator.dashboard_skill_matrix",
    "orchestrator.dashboard_state",
    "orchestrator.dashboard_widgets",
)


class DashboardFacadeHooks(NamedTuple):
    resolve_export: Any
    exported_dir: Any
    main: Any
    analytics_read: Any
    analytics: Any


def _evict_dashboard_modules() -> None:
    package = sys.modules.get("orchestrator")
    for module_name in _RELOADABLE_MODULES:
        sys.modules.pop(module_name, None)
        if package is not None:
            package.__dict__.pop(module_name.rsplit(".", 1)[-1], None)


def _build_export_hooks(facade_name: str) -> tuple[Any, Any]:
    exports = importlib.import_module("orchestrator._dashboard_exports")
    facade_namespace = None if facade_name in sys.modules else {}
    return exports.build_dashboard_exports(facade_name, facade_namespace)


def bootstrap_dashboard(
    module_file: str,
    facade_name: str,
    package_name: str,
) -> DashboardFacadeHooks:
    """Prepare import paths, lazy hooks, and direct-launch entrypoints."""
    launch_module_name = (
        "orchestrator.script_launch" if package_name else "script_launch"
    )
    launch_module = importlib.import_module(launch_module_name)
    launch_module.ensure_repo_root_on_path(module_file)
    _evict_dashboard_modules()
    resolve_export, exported_dir = _build_export_hooks(facade_name)
    return DashboardFacadeHooks(
        resolve_export,
        exported_dir,
        resolve_export("main"),
        resolve_export("analytics_read"),
        resolve_export("analytics"),
    )
