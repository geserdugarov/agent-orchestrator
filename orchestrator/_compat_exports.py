# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared lazy compatibility-export resolver."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional


ExportNamePair = tuple[str, Optional[str]]
ExportResolver = Callable[[str], Any]
ExportDirectory = Callable[[], list[str]]
ExportHooks = tuple[ExportResolver, ExportDirectory]


@dataclass(frozen=True)
class ExportTarget:
    """Location of one historical facade attribute."""

    export_name: str
    module_name: str
    target_name: Optional[str]


class CompatibilityExports:
    """Resolve and cache an immutable facade export inventory."""

    def __init__(
        self,
        facade_name: str,
        targets: Iterable[ExportTarget],
        exported_names: Optional[tuple[str, ...]],
    ) -> None:
        self._facade_name = facade_name
        self._targets = {target.export_name: target for target in targets}
        self._exported_names = exported_names

    def resolve(self, export_name: str) -> Any:
        """Resolve one registered attribute and cache it on the facade."""
        if export_name == "__all__" and self._exported_names is not None:
            resolved: Any = self._exported_names
        else:
            target = self._targets.get(export_name)
            if target is None:
                raise AttributeError(
                    f"module {self._facade_name!r} has no attribute {export_name!r}",
                )
            target_module = importlib.import_module(target.module_name)
            resolved = target_module if target.target_name is None else getattr(target_module, target.target_name)
        sys.modules[self._facade_name].__dict__[export_name] = resolved
        return resolved

    def exported_dir(self) -> list[str]:
        """Include registered lazy attributes in facade introspection."""
        facade_names = set(sys.modules[self._facade_name].__dict__)
        registered = set(self._targets)
        if self._exported_names is not None:
            registered.add("__all__")
        return sorted(facade_names | registered)


def export_group(
    module_name: str,
    name_pairs: Iterable[ExportNamePair],
) -> tuple[ExportTarget, ...]:
    """Build immutable targets that share one implementation module."""
    return tuple(
        ExportTarget(export_name, module_name, target_name)
        for export_name, target_name in name_pairs
    )


def build_exports(
    facade_name: str,
    targets: Iterable[ExportTarget],
    exported_names: Optional[tuple[str, ...]],
) -> ExportHooks:
    """Return module hooks backed by one compatibility registry."""
    registry = CompatibilityExports(facade_name, targets, exported_names)
    return registry.resolve, registry.exported_dir
