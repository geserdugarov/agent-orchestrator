# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Clean-process import, layering, and surface checks for the snapshot package."""

from __future__ import annotations

import subprocess
import sys
import unittest

from orchestrator.git import snapshots as _package
from orchestrator.git.snapshots import namespace, refs

_PACKAGE = "orchestrator.git.snapshots"

_MODULES = (
    _PACKAGE,
    f"{_PACKAGE}.namespace",
    f"{_PACKAGE}.refs",
)

_NAMESPACE_SCRIPT = """
import sys
import orchestrator.git.snapshots.namespace
print(*sorted(name for name in sys.modules if name.startswith('orchestrator')))
"""

# The names each owner defines. The initializer binds nothing, so a caller
# reaches the owner it needs and a test intercepting one targets that owner.
_OWNER_DEFINED = (
    ("SNAPSHOT_NAMESPACE", namespace),
    ("InvalidSnapshotRef", namespace),
    ("is_snapshot_ref", namespace),
    ("snapshot_ref", namespace),
    ("SnapshotOutcome", refs),
    ("create_snapshot_ref", refs),
    ("delete_snapshot_ref", refs),
    ("prove_snapshot_ref", refs),
)


class CleanProcessImportTest(unittest.TestCase):
    """Each module imports standalone in a fresh interpreter."""

    def test_each_module_imports_standalone(self) -> None:
        for module in _MODULES:
            with self.subTest(module=module):
                completed = subprocess.run(
                    [sys.executable, "-c", f"import {module}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, msg=completed.stderr)


class LayeringTest(unittest.TestCase):
    """The namespace is string policy and costs nothing to consult.

    It is read by the late domain's own lineage owner, which is charged for it
    on every pinned read a child's ancestry goes through -- so importing it
    must not drag the authenticated transport in behind it.
    """

    def test_the_namespace_reaches_no_transport(self) -> None:
        planted = subprocess.run(
            [sys.executable, "-c", _NAMESPACE_SCRIPT],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()

        self.assertNotIn("orchestrator.git.authentication", planted)
        self.assertNotIn("orchestrator.git.commands", planted)
        self.assertNotIn("orchestrator.config", planted)


class PackageSurfaceTest(unittest.TestCase):
    """The initializer is a marker; every name answers on its owner."""

    def test_initializer_binds_only_submodules(self) -> None:
        for name, bound in _package.__dict__.items():
            if name.startswith("__"):
                continue
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(bound, "__name__", None), f"{_PACKAGE}.{name}",
                )

    def test_each_name_answers_on_its_owner(self) -> None:
        for owner_name, owner in _OWNER_DEFINED:
            with self.subTest(name=owner_name):
                self.assertIn(owner_name, owner.__dict__)
                with self.assertRaises(AttributeError):
                    getattr(_package, owner_name)


if __name__ == "__main__":
    unittest.main()
