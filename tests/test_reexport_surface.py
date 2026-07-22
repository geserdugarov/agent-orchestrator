# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Compatibility-facade inventories, imports, identity, and patch routing."""
from __future__ import annotations

import ast
import importlib
import unittest
from itertools import chain
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from orchestrator import dashboard, workflow, worktrees
from orchestrator import _workflow_export_manifest
from orchestrator import _worktrees_export_manifest
from orchestrator.analytics import read as analytics_read


_FACADES = (workflow, worktrees, analytics_read, dashboard)
_STATIC_FACADES = (analytics_read, dashboard)
_PURE_STATIC_HUBS = (analytics_read,)
_LAZY_FACADES = (
    (workflow, _workflow_export_manifest),
    (worktrees, _worktrees_export_manifest),
)


def _intentional_reexports(node: ast.AST) -> tuple[str, ...]:
    if not isinstance(node, ast.ImportFrom) or node.module == "__future__":
        return ()
    return tuple(
        alias.name
        for alias in node.names
        if alias.asname == alias.name
    )


def _reexport_names(module) -> set[str]:
    """Names imported under the redundant-alias re-export marker (`X as X`).

    That alias is the pyflakes/ruff convention this repo uses to mark an
    intentional re-export; parsing it back out of the source gives the set of
    names the facade republishes, independent of the hand-maintained
    `__all__`.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return set(chain.from_iterable(map(_intentional_reexports, ast.walk(tree))))


def _lazy_targets(manifest) -> dict[str, object]:
    """Return the unique historical-name mapping declared by a manifest."""
    targets = {target.export_name: target for target in manifest.EXPORTS}
    if len(targets) != len(manifest.EXPORTS):
        raise AssertionError("lazy export manifest contains duplicate names")
    return targets


def _target_value(target):
    implementation = importlib.import_module(target.module_name)
    if target.target_name is None:
        return implementation
    return getattr(implementation, target.target_name)


class ReexportInventoryTest(unittest.TestCase):
    # The pure hubs re-export everything they expose, so `__all__` must equal
    # the re-export set exactly. `workflow` and `dashboard` also define their
    # own API (the dispatcher / the page entrypoint), so the re-export set is
    # only required to be a subset of their inventory.
    def test_all_is_sorted_and_unique(self) -> None:
        for module in _FACADES:
            with self.subTest(module=module.__name__):
                names = module.__all__
                self.assertEqual(
                    len(names), len(set(names)),
                    f"{module.__name__}.__all__ has duplicate entries",
                )
                self.assertEqual(
                    list(names), sorted(names),
                    f"{module.__name__}.__all__ is not sorted",
                )

    def test_every_listed_name_resolves(self) -> None:
        for module in _FACADES:
            for name in module.__all__:
                with self.subTest(module=module.__name__, name=name):
                    self.assertTrue(
                        hasattr(module, name),
                        f"{module.__name__}.__all__ lists {name!r} "
                        "but the module has no such attribute",
                    )

    def test_reexports_are_inventoried(self) -> None:
        for module in _STATIC_FACADES:
            with self.subTest(module=module.__name__):
                reexports = _reexport_names(module)
                listed = set(module.__all__)
                missing = reexports - listed
                self.assertEqual(
                    missing, set(),
                    f"{module.__name__} re-exports {sorted(missing)} but they "
                    "are absent from __all__",
                )
                if module in _PURE_STATIC_HUBS:
                    # A pure hub exposes only what it re-exports, so extras in
                    # __all__ would be dead entries.
                    self.assertEqual(
                        listed, reexports,
                        f"{module.__name__}.__all__ diverges from its "
                        f"re-export block: extra={sorted(listed - reexports)}",
                    )

    def test_lazy_facade_manifests_cover_star_exports(self) -> None:
        for module, manifest in _LAZY_FACADES:
            with self.subTest(module=module.__name__):
                targets = _lazy_targets(manifest)
                self.assertEqual(module.__all__, manifest.EXPORTED_NAMES)
                self.assertEqual(set(module.__all__) - set(targets), set())

    def test_lazy_facade_targets_preserve_identity_and_from_import(self) -> None:
        for module, manifest in _LAZY_FACADES:
            for name, target in _lazy_targets(manifest).items():
                with self.subTest(module=module.__name__, name=name):
                    expected = _target_value(target)
                    self.assertIs(getattr(module, name), expected)
                    imported = __import__(module.__name__, fromlist=(name,))
                    self.assertIs(getattr(imported, name), expected)

    def test_lazy_facade_wildcard_import_matches_inventory(self) -> None:
        for module, _manifest in _LAZY_FACADES:
            with self.subTest(module=module.__name__):
                namespace: dict[str, object] = {}
                exec(f"from {module.__name__} import *", namespace)
                namespace.pop("__builtins__", None)
                self.assertEqual(set(namespace), set(module.__all__))
                for name, value in namespace.items():
                    self.assertIs(value, getattr(module, name))

    def test_workflow_handler_patches_remain_late_bound(self) -> None:
        issue = SimpleNamespace(number=17)
        spec = SimpleNamespace(slug="owner/repo")
        handler = Mock()
        with patch.object(workflow, "_handle_ready", handler):
            workflow._route_issue_to_handler(None, spec, issue, "ready")
        handler.assert_called_once_with(None, spec, issue)


if __name__ == "__main__":
    unittest.main()
