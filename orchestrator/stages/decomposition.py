# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable decomposition, dependency, and umbrella handler surface."""
from __future__ import annotations

from orchestrator.stages import _decomposition_exports

__dir__ = _decomposition_exports.exported_dir
__getattr__ = _decomposition_exports.resolve_export
