# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The compatibility facades (`workflow`, `worktrees`, `analytics.read`,
`dashboard`) each carry an explicit `__all__` inventory of the surface they
re-export. These tests keep that inventory honest so the re-export blocks stay
auditable: every listed name must resolve, the list must be sorted and
duplicate-free, and -- for the pure re-export hubs -- it must match the set of
names actually imported under the `X as X` re-export marker, so a helper added
to (or dropped from) the import block cannot silently drift out of `__all__`.
"""
from __future__ import annotations

import ast
import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import dashboard, workflow, worktrees
from orchestrator.analytics import read as analytics_read


def _reexport_names(module) -> set[str]:
    """Names imported under the redundant-alias re-export marker (`X as X`).

    That alias is the pyflakes/ruff convention this repo uses to mark an
    intentional re-export; parsing it back out of the source gives the set of
    names the facade republishes, independent of the hand-maintained
    `__all__`.
    """
    tree = ast.parse(open(module.__file__).read())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                if alias.asname is not None and alias.asname == alias.name:
                    names.add(alias.name)
    return names


class ReexportInventoryTest(unittest.TestCase):
    FACADES = (workflow, worktrees, analytics_read, dashboard)
    # The pure hubs re-export everything they expose, so `__all__` must equal
    # the re-export set exactly. `workflow` and `dashboard` also define their
    # own API (the dispatcher / the page entrypoint), so the re-export set is
    # only required to be a subset of their inventory.
    PURE_HUBS = (worktrees, analytics_read)

    def test_all_is_sorted_and_unique(self) -> None:
        for module in self.FACADES:
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
        for module in self.FACADES:
            for name in module.__all__:
                with self.subTest(module=module.__name__, name=name):
                    self.assertTrue(
                        hasattr(module, name),
                        f"{module.__name__}.__all__ lists {name!r} "
                        "but the module has no such attribute",
                    )

    def test_reexports_are_inventoried(self) -> None:
        for module in self.FACADES:
            with self.subTest(module=module.__name__):
                reexports = _reexport_names(module)
                listed = set(module.__all__)
                missing = reexports - listed
                self.assertEqual(
                    missing, set(),
                    f"{module.__name__} re-exports {sorted(missing)} but they "
                    "are absent from __all__",
                )
                if module in self.PURE_HUBS:
                    # A pure hub exposes only what it re-exports, so extras in
                    # __all__ would be dead entries.
                    self.assertEqual(
                        listed, reexports,
                        f"{module.__name__}.__all__ diverges from its "
                        f"re-export block: extra={sorted(listed - reexports)}",
                    )


if __name__ == "__main__":
    unittest.main()
