# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable facade for worktree paths, creation, recovery, and cleanup."""
from __future__ import annotations

from orchestrator import _worktree_lifecycle_exports

__dir__ = _worktree_lifecycle_exports.exported_dir
__getattr__ = _worktree_lifecycle_exports.resolve_export
