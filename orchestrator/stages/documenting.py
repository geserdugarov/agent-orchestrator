# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable final-documentation handler and helper surface."""
from __future__ import annotations

from orchestrator.stages import _documenting_exports

__dir__ = _documenting_exports.exported_dir
__getattr__ = _documenting_exports.resolve_export
