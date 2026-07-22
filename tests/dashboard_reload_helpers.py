# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Hermetic dashboard reload and dependency lookup helpers."""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType
from unittest.mock import patch


SKIP_DOTENV_ENV = "ORCHESTRATOR_SKIP_DOTENV"
TOKEN_FILE_ENV = "ORCHESTRATOR_TOKEN_FILE"
MISSING_TOKEN_FILE = "/tmp/agent-orchestrator-token-missing"
ANALYTICS_READ_MODULE = "orchestrator.analytics.read"
DASHBOARD_MODULE = "orchestrator.dashboard"
THEME_MODULE = "orchestrator.dashboard_theme"
_RELOAD_POP_MODULES = (
    "orchestrator.config",
    ANALYTICS_READ_MODULE,
    "orchestrator.analytics",
    "orchestrator.dashboard_state",
    "orchestrator.dashboard_kpis",
    "orchestrator.dashboard_html",
    "orchestrator.dashboard_cards",
    "orchestrator.dashboard_kpi_strip",
    "orchestrator.dashboard_skill_adoption",
    "orchestrator.dashboard_skill_matrix",
    "orchestrator.dashboard_reads",
    "orchestrator.dashboard_widgets",
    DASHBOARD_MODULE,
)


def hermetic_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the import-time environment shared by dashboard tests."""
    environment = {
        SKIP_DOTENV_ENV: "1",
        TOKEN_FILE_ENV: MISSING_TOKEN_FILE,
    }
    if extra:
        environment.update(extra)
    return environment


def reload_dashboard(
    environment: dict[str, str] | None = None,
) -> tuple[ModuleType, ModuleType]:
    """Load analytics and every dashboard leaf against one environment."""
    with patch.dict(os.environ, hermetic_environment(environment), clear=True):
        for module_name in _RELOAD_POP_MODULES:
            sys.modules.pop(module_name, None)
        analytics = importlib.import_module("orchestrator.analytics")
        dashboard = importlib.import_module(DASHBOARD_MODULE)
    return analytics, dashboard


def load_analytics_read() -> ModuleType:
    """Return the analytics read facade bound to the current import world."""
    return importlib.import_module(ANALYTICS_READ_MODULE)


def load_dashboard_theme() -> ModuleType:
    """Return the dashboard theme module for color-token assertions."""
    return importlib.import_module(THEME_MODULE)
