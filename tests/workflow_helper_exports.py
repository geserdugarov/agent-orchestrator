# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Lazy compatibility hooks for shared workflow test helpers."""
from __future__ import annotations

from orchestrator._compat_exports import build_exports

from tests.workflow_helper_export_manifest import EXPORTS


resolve_export, exported_dir = build_exports(
    "tests.workflow_helpers",
    EXPORTS,
    None,
)
