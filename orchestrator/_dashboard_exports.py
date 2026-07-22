# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Lazy compatibility hooks for :mod:`orchestrator.dashboard`."""
from __future__ import annotations

from typing import Any, MutableMapping, Optional

from orchestrator._compat_exports import ExportHooks, build_exports
from orchestrator._dashboard_export_manifest import EXPORTED_NAMES, EXPORTS


def build_dashboard_exports(
    facade_name: str,
    facade_namespace: Optional[MutableMapping[str, Any]] = None,
) -> ExportHooks:
    """Build hooks that also support Streamlit's direct script launch."""
    return build_exports(
        facade_name,
        EXPORTS,
        EXPORTED_NAMES,
        facade_namespace,
    )
