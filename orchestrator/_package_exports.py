# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Lazy compatibility exports for the root package."""
from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

_EXPORTED_NAMES = ("__version__",)
_PACKAGE_VERSION = "0.7.0"


def _package_module() -> ModuleType:
    """Return the root package whose namespace owns the lazy exports."""
    return sys.modules["orchestrator"]


def resolve_export(export_name: str) -> Any:
    """Resolve and cache the historical root-package metadata surface."""
    if export_name == "__all__":
        exported_value: Any = _EXPORTED_NAMES
    elif export_name == "__version__":
        exported_value = _PACKAGE_VERSION
    else:
        raise AttributeError(f"module 'orchestrator' has no attribute {export_name!r}")
    _package_module().__dict__[export_name] = exported_value
    return exported_value


def exported_dir() -> list[str]:
    """Include lazy compatibility names in package introspection."""
    package_names = set(_package_module().__dict__)
    return sorted(package_names | set(_EXPORTED_NAMES) | {"__all__"})


__getattr__ = resolve_export
__dir__ = exported_dir
