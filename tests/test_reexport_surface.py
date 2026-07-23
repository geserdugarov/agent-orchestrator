# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Compatibility-facade inventories and static surfaces."""
from __future__ import annotations

import unittest

from tests.reexport_test_facades import (
    FACADES,
    LAZY_FACADES,
    PURE_STATIC_HUBS,
    STATIC_FACADES,
    STUBBED_FACADES,
)
from tests.reexport_test_support import (
    lazy_targets,
    reexport_names,
    stub_names,
)


class ReexportInventoryTest(unittest.TestCase):
    def test_all_is_sorted_and_unique(self) -> None:
        for module in FACADES:
            with self.subTest(module=module.__name__):
                names = module.__all__
                self.assertEqual(
                    len(names),
                    len(set(names)),
                    f"{module.__name__}.__all__ has duplicate entries",
                )
                self.assertEqual(
                    list(names),
                    sorted(names),
                    f"{module.__name__}.__all__ is not sorted",
                )

    def test_every_listed_name_resolves(self) -> None:
        for module in FACADES:
            for name in module.__all__:
                with self.subTest(module=module.__name__, name=name):
                    self.assertTrue(
                        hasattr(module, name),
                        f"{module.__name__}.__all__ lists {name!r} "
                        "but the module has no such attribute",
                    )

    def test_stub_matches_runtime_inventory(self) -> None:
        for module in STUBBED_FACADES:
            with self.subTest(module=module.__name__):
                names = stub_names(module)
                names.discard("__all__")
                self.assertEqual(names, set(module.__all__))

    def test_reexports_are_inventoried(self) -> None:
        for module in STATIC_FACADES:
            with self.subTest(module=module.__name__):
                reexports = reexport_names(module)
                listed = set(module.__all__)
                missing = reexports - listed
                self.assertEqual(
                    missing,
                    set(),
                    f"{module.__name__} re-exports {sorted(missing)} but "
                    "they are absent from __all__",
                )
                if module in PURE_STATIC_HUBS:
                    self.assertEqual(listed, reexports)

    def test_manifest_covers_star_exports(self) -> None:
        for module, manifest in LAZY_FACADES:
            with self.subTest(module=module.__name__):
                targets = lazy_targets(manifest)
                self.assertEqual(module.__all__, manifest.EXPORTED_NAMES)
                self.assertEqual(
                    set(module.__all__) - set(targets),
                    set(),
                )
