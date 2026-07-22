# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Lazy compatibility hooks for :mod:`orchestrator.analytics`."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from orchestrator.analytics._package_initialization import initialize_package
from orchestrator.analytics._package_manifest import EXPORTED_NAMES


def _package_module() -> ModuleType:
    return sys.modules["orchestrator.analytics"]


def resolve_export(export_name: str) -> Any:
    """Resolve and cache the complete analytics package surface."""
    package = _package_module()
    if not package.__dict__.get("_ANALYTICS_EXPORTS_INITIALIZED"):
        initialize_package(package)
    try:
        return package.__dict__[export_name]
    except KeyError as error:
        raise AttributeError(
            f"module 'orchestrator.analytics' has no attribute {export_name!r}",
        ) from error


def exported_dir() -> list[str]:
    """Include lazy analytics compatibility names in package introspection."""
    package_names = set(_package_module().__dict__)
    return sorted(package_names | set(EXPORTED_NAMES) | {"__all__"})


__getattr__ = resolve_export
__dir__ = exported_dir
