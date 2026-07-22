# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable manual-review feedback and watermark handler surface."""
from __future__ import annotations

from orchestrator.stages import _in_review_exports

__dir__ = _in_review_exports.exported_dir
__getattr__ = _in_review_exports.resolve_export
