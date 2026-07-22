# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable facade for hardened local and authenticated git operations."""
from __future__ import annotations

from orchestrator import _git_plumbing_exports

__dir__ = _git_plumbing_exports.exported_dir
__getattr__ = _git_plumbing_exports.resolve_export
