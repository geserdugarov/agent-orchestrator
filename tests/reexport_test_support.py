# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Source, manifest, and runtime helpers for facade compatibility tests."""
from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from itertools import chain
from pathlib import Path


@dataclass(frozen=True)
class ResolvedTarget:
    expected: object
    direct: object
    imported: object


def intentional_reexports(node: ast.AST) -> tuple[str, ...]:
    if not isinstance(node, ast.ImportFrom) or node.module == "__future__":
        return ()
    return tuple(
        alias.name
        for alias in node.names
        if alias.asname == alias.name
    )


def reexport_names(module) -> set[str]:
    tree = ast.parse(
        Path(module.__file__).read_text(encoding="utf-8"),
    )
    return set(chain.from_iterable(
        map(intentional_reexports, ast.walk(tree)),
    ))


def lazy_targets(manifest) -> dict[str, object]:
    targets = {
        target.export_name: target
        for target in manifest.EXPORTS
    }
    if len(targets) != len(manifest.EXPORTS):
        raise AssertionError(
            "lazy export manifest contains duplicate names",
        )
    return targets


def target_value(target):
    implementation = importlib.import_module(target.module_name)
    if target.target_name is None:
        return implementation
    return getattr(implementation, target.target_name)


def stub_names(module) -> set[str]:
    stub_path = Path(module.__file__).with_suffix(".pyi")
    tree = ast.parse(stub_path.read_text(encoding="utf-8"))
    return {
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
    }


def resolve_target(module, name: str, target) -> ResolvedTarget:
    module.__dict__.pop(name, None)
    expected = target_value(target)
    imported_module = importlib.import_module(module.__name__)
    return ResolvedTarget(
        expected=expected,
        direct=getattr(module, name),
        imported=getattr(imported_module, name),
    )
