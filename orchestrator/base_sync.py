# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable facade for base refresh, rebase recovery, and conflict routing."""
from __future__ import annotations

from orchestrator import _base_sync_exports

__dir__ = _base_sync_exports.exported_dir
__getattr__ = _base_sync_exports.resolve_export
