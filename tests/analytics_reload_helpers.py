# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Hermetic analytics-package reload support for recording tests."""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from contextlib import ExitStack
from types import ModuleType
from unittest.mock import patch


_MODULE_PREFIX = "orchestrator.analytics"
_CONFIG_MODULE = "orchestrator.config"
_MISSING = object()


@dataclass(frozen=True)
class _ModuleSnapshot:
    modules: dict[str, ModuleType]
    package_attributes: dict[str, object]


def _hermetic_env(extra: dict[str, str] | None) -> dict[str, str]:
    environment = {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
    }
    if extra:
        environment.update(extra)
    return environment


def _snapshot(package: ModuleType) -> _ModuleSnapshot:
    modules = {
        name: module
        for name, module in sys.modules.items()
        if name == _CONFIG_MODULE or name.startswith(_MODULE_PREFIX)
    }
    attributes = {name: package.__dict__.get(name, _MISSING) for name in ("analytics", "config")}
    return _ModuleSnapshot(modules, attributes)


def _clear(package: ModuleType) -> None:
    for module_name in tuple(sys.modules):
        if module_name == _CONFIG_MODULE or module_name.startswith(_MODULE_PREFIX):
            sys.modules.pop(module_name, None)
    package.__dict__.pop("analytics", None)
    package.__dict__.pop("config", None)


def _restore(package: ModuleType, snapshot: _ModuleSnapshot) -> None:
    _clear(package)
    sys.modules.update(snapshot.modules)
    for name, member in snapshot.package_attributes.items():
        if member is not _MISSING:
            package.__dict__[name] = member


def reload_analytics(
    environment: dict[str, str] | None = None,
) -> tuple[ModuleType, ModuleType]:
    """Load a fresh analytics world and restore the process import world."""
    package = importlib.import_module("orchestrator")
    snapshot = _snapshot(package)
    with ExitStack() as cleanup:
        cleanup.callback(_restore, package, snapshot)
        with patch.dict(os.environ, _hermetic_env(environment), clear=True):
            _clear(package)
            config = importlib.import_module(_CONFIG_MODULE)
            analytics = importlib.import_module(_MODULE_PREFIX)
    return config, analytics
