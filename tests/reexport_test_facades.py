# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Facade and manifest inventories used by compatibility tests."""
from __future__ import annotations

import importlib

from orchestrator import _dashboard_export_manifest
from orchestrator import _workflow_export_manifest
from orchestrator import _worktrees_export_manifest
from orchestrator.analytics import _read_export_manifest


ANALYTICS_FACADE = importlib.import_module("orchestrator.analytics")
DASHBOARD_FACADE = importlib.import_module("orchestrator.dashboard")
WORKFLOW_FACADE = importlib.import_module("orchestrator.workflow")
WORKTREES_FACADE = importlib.import_module("orchestrator.worktrees")
ANALYTICS_READ_FACADE = importlib.import_module(
    "orchestrator.analytics.read",
)

FACADES = (
    ANALYTICS_FACADE,
    WORKFLOW_FACADE,
    WORKTREES_FACADE,
    ANALYTICS_READ_FACADE,
    DASHBOARD_FACADE,
)
STUBBED_FACADES = (
    ANALYTICS_FACADE,
    ANALYTICS_READ_FACADE,
    DASHBOARD_FACADE,
)
STATIC_FACADES = ()
PURE_STATIC_HUBS = ()
LAZY_FACADES = (
    (WORKFLOW_FACADE, _workflow_export_manifest),
    (WORKTREES_FACADE, _worktrees_export_manifest),
    (ANALYTICS_READ_FACADE, _read_export_manifest),
    (DASHBOARD_FACADE, _dashboard_export_manifest),
)
