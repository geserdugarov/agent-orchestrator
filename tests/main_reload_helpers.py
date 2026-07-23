# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Hermetic entry-point reload support for polling-loop tests."""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from unittest.mock import patch


_CONFIG_MODULE = "orchestrator.config"
_MAIN_MODULE = "orchestrator.main"
_MODULE_NAMES = (_CONFIG_MODULE, _MAIN_MODULE)
_MISSING = object()
_BASE_ENV = MappingProxyType(
    {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        "GITHUB_TOKEN": "ghp-test-secret",
    }
)


@dataclass(frozen=True)
class _MainSnapshot:
    modules: dict[str, ModuleType]
    package_attributes: dict[str, object]


def _snapshot(package: ModuleType) -> _MainSnapshot:
    modules = {}
    for name in _MODULE_NAMES:
        module = sys.modules.get(name)
        if module is not None:
            modules[name] = module
    attributes = {
        attribute_name: package.__dict__.get(attribute_name, _MISSING)
        for attribute_name in ("config", "main")
    }
    return _MainSnapshot(modules, attributes)


def _clear(package: ModuleType) -> None:
    sys.modules.pop(_CONFIG_MODULE, None)
    sys.modules.pop(_MAIN_MODULE, None)
    package.__dict__.pop("config", None)
    package.__dict__.pop("main", None)


def _restore(package: ModuleType, snapshot: _MainSnapshot) -> None:
    _clear(package)
    sys.modules.update(snapshot.modules)
    for name, member in snapshot.package_attributes.items():
        if member is not _MISSING:
            package.__dict__[name] = member


@contextmanager
def reload_main(environment: dict[str, str]):
    """Load the entry point with isolated environment and import state."""
    package = importlib.import_module("orchestrator")
    snapshot = _snapshot(package)
    full_environment = dict(_BASE_ENV)
    full_environment.update(environment)
    with ExitStack() as cleanup:
        cleanup.callback(_restore, package, snapshot)
        with patch.dict(os.environ, full_environment, clear=True):
            _clear(package)
            importlib.import_module(_CONFIG_MODULE)
            main_module = importlib.import_module(_MAIN_MODULE)
            with (
                patch.object(main_module, "_configure_logging"),
                patch.object(
                    main_module.signal,
                    "signal",
                ),
            ):
                yield main_module
