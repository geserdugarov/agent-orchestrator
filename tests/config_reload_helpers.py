# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Hermetic configuration-module loading for configuration tests."""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from unittest.mock import patch


_CONFIG_MODULE = "orchestrator.config"
_MISSING = object()
_BASE_ENV = MappingProxyType(
    {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
    }
)


@dataclass(frozen=True)
class _ConfigSnapshot:
    module: ModuleType | None
    package_attribute: object


def _clear_config(package: ModuleType) -> None:
    sys.modules.pop(_CONFIG_MODULE, None)
    package.__dict__.pop("config", None)


def _restore_config(package: ModuleType, snapshot: _ConfigSnapshot) -> None:
    _clear_config(package)
    if snapshot.module is not None:
        sys.modules[_CONFIG_MODULE] = snapshot.module
    if snapshot.package_attribute is not _MISSING:
        package.__dict__["config"] = snapshot.package_attribute


def load_config(environment: dict[str, str] | None = None) -> ModuleType:
    """Import configuration against an isolated environment and import cache."""
    package = importlib.import_module("orchestrator")
    snapshot = _ConfigSnapshot(
        sys.modules.get(_CONFIG_MODULE),
        package.__dict__.get("config", _MISSING),
    )
    full_environment = dict(_BASE_ENV)
    if environment:
        full_environment.update(environment)
    with ExitStack() as cleanup:
        cleanup.callback(_restore_config, package, snapshot)
        with patch.dict(os.environ, full_environment, clear=True):
            _clear_config(package)
            return importlib.import_module(_CONFIG_MODULE)


def config_error_message(environment: dict[str, str]) -> str:
    """Return the import-time configuration error for an invalid environment."""
    try:
        load_config(environment)
    except SystemExit as error:
        return str(error)
    raise AssertionError("configuration import did not fail")
