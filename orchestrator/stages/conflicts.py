# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable resolving-conflict handler and helper surface."""
from __future__ import annotations

from orchestrator.stages import _conflicts_exports

__dir__ = _conflicts_exports.exported_dir
__getattr__ = _conflicts_exports.resolve_export
