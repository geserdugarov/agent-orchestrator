# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable analytics read surface backed by focused lazy leaves."""

from __future__ import annotations

from orchestrator.analytics import _read_exports

__dir__ = _read_exports.exported_dir
__getattr__ = _read_exports.resolve_export
