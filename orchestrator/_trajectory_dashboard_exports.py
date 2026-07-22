# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Lazy compatibility hooks for the trajectory-viewer facade."""
from __future__ import annotations

from typing import Any, MutableMapping, Optional

from orchestrator._compat_exports import ExportHooks, build_exports
from orchestrator._trajectory_dashboard_export_manifest import EXPORTS


def build_trajectory_dashboard_exports(
    facade_name: str,
    facade_namespace: Optional[MutableMapping[str, Any]] = None,
) -> ExportHooks:
    return build_exports(facade_name, EXPORTS, None, facade_namespace)
