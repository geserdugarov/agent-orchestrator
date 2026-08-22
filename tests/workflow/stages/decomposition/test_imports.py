# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Clean-process import, layering, and surface checks for the stage package."""

from __future__ import annotations

import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from types import MappingProxyType

from orchestrator.workflow.engine import dispatch as _dispatch
from orchestrator.workflow.stages import decomposition as _package
from orchestrator.workflow.state import WorkflowLabel

_PACKAGE = "orchestrator.workflow.stages.decomposition"

_PARENT = "orchestrator.workflow.stages"

_BLOCKED = "blocked"

_OWNERS = (
    "activation",
    _BLOCKED,
    "late_children",
    "late_cleanup",
    "late_content",
    "late_coordinator",
    "late_guidance",
    "late_hold",
    "late_models",
    "late_notice",
    "late_outcome",
    "late_owner",
    "late_prompt",
    "late_relabel",
    "late_reply",
    "late_revision",
    "late_session",
    "late_settlement",
    "late_snapshot",
    "late_transaction",
    "manifest",
    "models",
    "outcomes",
    "parents",
    "recovery",
    "run",
    "session",
    "split",
    "state",
    "umbrella",
    "validation",
)

# Bound at module scope, so collecting this file is what plants every owner in
# `sys.modules` -- the same protection each sibling owner package gets from its
# own import test, and what keeps an owner from being first imported by a test
# that has already reloaded the modules it binds.
_OWNER_MODULES = MappingProxyType({
    owner: importlib.import_module(f"{_PACKAGE}.{owner}") for owner in _OWNERS
})

_IMPORTED_SCRIPT = """
import sys
import {module}
print(*sorted(name for name in sys.modules if name.startswith('orchestrator')))
"""

# The label -> owner pairs the dispatcher routes a decomposed issue through.
_DISPATCHED_HANDLERS = (
    (WorkflowLabel.DECOMPOSING, "run", "_handle_decomposing"),
    (WorkflowLabel.READY, _BLOCKED, "_handle_ready"),
    (WorkflowLabel.BLOCKED, _BLOCKED, "_handle_blocked"),
    (WorkflowLabel.UMBRELLA, "umbrella", "_handle_umbrella"),
)


def _imported_orchestrator_modules(module: str) -> set[str]:
    """Names of the orchestrator modules a fresh `import module` plants."""
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORTED_SCRIPT.format(module=module)],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(completed.stdout.split())


class CleanProcessImportTest(unittest.TestCase):
    """The package and each owner beneath it import alone.

    Every owner reaches the engine, whose dispatcher reaches back into this
    package. A subprocess per module gives each a clean `sys.modules` no other
    test has already populated, exposing an import-order cycle a package-first
    suite run would mask.
    """

    def test_each_module_imports_standalone(self) -> None:
        for module in (_PACKAGE, *(f"{_PACKAGE}.{owner}" for owner in _OWNERS)):
            with self.subTest(module=module):
                completed = subprocess.run(
                    [sys.executable, "-c", f"import {module}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, msg=completed.stderr)


class LayeringTest(unittest.TestCase):
    """The initializer costs the package above it and nothing else."""

    def test_initializer_reaches_no_owner(self) -> None:
        # An eager owner binding here would charge the `blocked` walk for the
        # manifest parser and the split writer it never reaches -- and for the
        # worktree, GitHub, and analytics subsystems those sit on.
        self.assertEqual(
            _imported_orchestrator_modules(_PACKAGE),
            _imported_orchestrator_modules(_PARENT) | {_PACKAGE},
        )


class PackageSurfaceTest(unittest.TestCase):
    """The initializer is a package marker that owns no names."""

    def test_package_sits_under_the_stage_package(self) -> None:
        self.assertEqual(_package.__name__, _PACKAGE)
        initializer = Path(_package.__file__)
        self.assertEqual(initializer.name, "__init__.py")
        self.assertEqual(initializer.parent.name, "decomposition")
        self.assertIs(importlib.import_module(_PARENT).decomposition, _package)

    def test_initializer_binds_only_submodules(self) -> None:
        for owner, module in _OWNER_MODULES.items():
            with self.subTest(owner=owner):
                self.assertIs(getattr(_package, owner), module)
        for name, bound in _package.__dict__.items():
            if name.startswith("__"):
                continue
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(bound, "__name__", None), f"{_PACKAGE}.{name}",
                )


class DispatchTargetTest(unittest.TestCase):
    """The dispatcher names the owner each handler lives on."""

    def test_each_label_resolves_to_its_owner(self) -> None:
        # A dispatched handler is resolved off the module the table names -- so
        # that is where a patch has to land to intercept one, for every one of
        # the four labels this stage answers for.
        for label, owner_name, handler_name in _DISPATCHED_HANDLERS:
            with self.subTest(label=label):
                owner = _OWNER_MODULES[owner_name]
                self.assertEqual(
                    _dispatch._STAGE_HANDLER_TARGETS[label],
                    (owner.__name__, handler_name),
                )
                self.assertTrue(hasattr(owner, handler_name))


if __name__ == "__main__":
    unittest.main()
