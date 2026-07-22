# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable workflow surface backed by responsibility-focused lazy leaves."""
from __future__ import annotations

from orchestrator import _workflow_dependencies, _workflow_exports

analytics = _workflow_dependencies.analytics
config = _workflow_dependencies.config

__dir__ = _workflow_exports.exported_dir
__getattr__ = _workflow_exports.resolve_export
