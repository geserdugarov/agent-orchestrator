# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable read-only question-session handler and helper surface."""
from __future__ import annotations

from orchestrator.stages import _question_exports

__dir__ = _question_exports.exported_dir
__getattr__ = _question_exports.resolve_export
