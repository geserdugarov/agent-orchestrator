# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Package and direct-launch bootstrap for the trajectory viewer."""
from __future__ import annotations

import importlib
import sys
from typing import Any, NamedTuple


class TrajectoryDashboardHooks(NamedTuple):
    resolve_export: Any
    exported_dir: Any
    main: Any
    trajectory_reader: Any


def bootstrap_trajectory_dashboard(
    module_file: str,
    facade_name: str,
    package_name: str,
) -> TrajectoryDashboardHooks:
    importlib.import_module(
        "orchestrator.script_launch" if package_name else "script_launch",
    ).ensure_repo_root_on_path(module_file)
    exports = importlib.import_module("orchestrator._trajectory_dashboard_exports")
    facade_namespace = None if facade_name in sys.modules else {}
    resolve_export, exported_dir = exports.build_trajectory_dashboard_exports(
        facade_name,
        facade_namespace,
    )
    return TrajectoryDashboardHooks(
        resolve_export,
        exported_dir,
        resolve_export("main"),
        resolve_export("trajectory_reader"),
    )
