# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable review-feedback fixing handler and helper surface."""
from __future__ import annotations

from orchestrator.stages import _fixing_exports

__dir__ = _fixing_exports.exported_dir
__getattr__ = _fixing_exports.resolve_export
