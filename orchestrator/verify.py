# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable facade for local verification and worktree state probes."""
from __future__ import annotations

from orchestrator import _verify_exports

__dir__ = _verify_exports.exported_dir
__getattr__ = _verify_exports.resolve_export
