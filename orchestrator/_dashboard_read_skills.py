# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard per-skill read wrappers."""
from __future__ import annotations

from orchestrator.analytics import read as analytics_read
from orchestrator._dashboard_read_core import _read_filtered


def _read_skill_trigger_matrix(key: tuple):
    return _read_filtered(analytics_read.get_skill_trigger_matrix, key)


def _read_skill_adoption(key: tuple):
    return _read_filtered(analytics_read.get_skill_adoption, key)
