# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Clean-process import, layering, and package-surface checks for base sync."""

from __future__ import annotations

import subprocess
import sys
import unittest
from importlib.util import find_spec

from orchestrator.git import base_sync

_MODELS_OWNER = "orchestrator.git.base_sync.models"

_PRE_PR_OWNER = "orchestrator.git.base_sync.pre_pr"

_REFRESH_OWNER = "orchestrator.git.base_sync.refresh"

_STATE_OWNER = "orchestrator.git.base_sync.state"

_PERSISTENCE_OWNER = "orchestrator.git.base_sync.persistence"

_OUTCOMES_OWNER = "orchestrator.git.base_sync.outcomes"

_SNAPSHOT_OWNER = "orchestrator.git.base_sync.snapshot"

_RECOVERY_OWNER = "orchestrator.git.base_sync.recovery"

_STARTUP_OWNER = "orchestrator.git.base_sync.startup"

_ELIGIBILITY_OWNER = "orchestrator.git.base_sync.eligibility"

_PUBLICATION_OWNER = "orchestrator.git.base_sync.publication"

_GUARDS_OWNER = "orchestrator.git.base_sync.guards"

_PR_OWNER = "orchestrator.git.base_sync.pr"

_CONFLICTS_OWNER = "orchestrator.git.base_sync.conflicts"

_FROZEN_OWNER = "orchestrator.git.base_sync.frozen"

_TRANSFERS_OWNER = "orchestrator.git.base_sync.transfers"

_OWNERS = (
    _MODELS_OWNER, _PRE_PR_OWNER, _REFRESH_OWNER, _STATE_OWNER,
    _PERSISTENCE_OWNER, _OUTCOMES_OWNER, _SNAPSHOT_OWNER, _RECOVERY_OWNER,
    _STARTUP_OWNER, _ELIGIBILITY_OWNER, _PUBLICATION_OWNER, _GUARDS_OWNER,
    _PR_OWNER, _CONFLICTS_OWNER, _FROZEN_OWNER, _TRANSFERS_OWNER,
)

_MODULES = ("orchestrator.git.base_sync", *_OWNERS)

# The module paths a second import site for these owners would take: the flat
# spelling itself, and the inventory and resolver hooks one would be built from.
_FLAT_MODULES = (
    "orchestrator._base_sync_export_manifest",
    "orchestrator._base_sync_exports",
    "orchestrator.base_sync",
)

# The state owner exists to spell out the pinned-state keys and the label
# vocabulary one rebase attempt is routed by, so the workflow package's `state`
# owner -- plus the initializer an import of it runs -- are the only
# orchestrator modules it may reach. Every owner is typed by that vocabulary, so
# this is also the exempt set the forbidden-prefix check below drops before it
# looks for an inverted dependency. The pre-PR owner adds only the git envelope
# its rebases run under and the repository spec they read their base ref off.
_ALLOWED_MODULES = (
    "orchestrator",
    "orchestrator.workflow",
    "orchestrator.workflow.state",
)

_ALLOWED_ROOTS = (
    (_STATE_OWNER, ("orchestrator.git",)),
    (_PRE_PR_OWNER, ("orchestrator.config", "orchestrator.git")),
)

# Every owner outside that layer annotates its fields and arguments with the
# composed GitHub client, which drags the analytics and usage graph in behind
# it, so an allowlist would not describe them. What every owner owes is the
# direction of the dependency: none may reach the workflow engine and its stage
# handlers, or an application entrypoint. These prefixes catch that past the
# label owner the exempt set above allows. The collaborators that do live above
# this package -- the park guard and the comment poster in the workflow
# engine -- are reached through call-time imports, which is what keeps them out
# of this check.
_FORBIDDEN_PREFIXES = (
    "orchestrator.cli",
    "orchestrator.runtime",
    "orchestrator.workflow",
)

_LAYERING_SCRIPT = """
import sys
import {module}
print(*sorted(name for name in sys.modules if name.startswith('orchestrator')))
"""

# The initializer binds nothing, so each name stays reachable only through the
# owner that defines it.
_OWNER_ONLY_NAMES = (
    "_AUTO_REBASE_PARK_REASONS",
    "_AutoRebaseContext",
    "_AutoRebaseRequest",
    "_PENDING_PUSH_SHA",
    "_auto_rebase_retry_decision",
    "_fetch_recovery_snapshot",
    "_park_dirty_recovery",
    "_publish_auto_rebase",
    "_recover_pending_auto_base_rebase",
    "_rewritten_by_the_rebase",
    "_refresh_base_and_worktrees",
    "_reset_clear_and_park",
    "_route_pr_worktree_to_resolving_conflict",
    "_start_auto_rebase",
    "_sync_pr_worktree_to_base",
    "log",
)


def _imported_orchestrator_modules(module: str) -> list[str]:
    """Names of the orchestrator modules a fresh `import module` pulls in."""
    completed = subprocess.run(
        [sys.executable, "-c", _LAYERING_SCRIPT.format(module=module)],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.split()


class CleanProcessImportTest(unittest.TestCase):
    """Each base-sync module imports standalone in a fresh interpreter.

    The owners bind their collaborators at import time, so importing any one
    of them first must not need a name a half-run module has not defined yet.
    A subprocess per module gives each a clean `sys.modules` no other test has
    already populated, exposing an import-order cycle a suite run that always
    reaches the owners in the same order would mask.
    """

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
    """The owners import nothing from the base-sync leaves or above them."""

    def test_lower_owners_stay_in_their_layer(self) -> None:
        for owner, allowed_roots in _ALLOWED_ROOTS:
            with self.subTest(module=owner):
                for imported in _imported_orchestrator_modules(owner):
                    self.assertTrue(
                        self._within_layers(imported, allowed_roots),
                        f"{owner} reaches past its layer via {imported}",
                    )

    def test_owners_stay_below_base_sync_leaves(self) -> None:
        for module in _OWNERS:
            with self.subTest(module=module):
                for imported in self._imports_past_the_label_owner(module):
                    self.assertFalse(
                        imported.startswith(_FORBIDDEN_PREFIXES),
                        f"{module} inverts the dependency via {imported}",
                    )

    def _imports_past_the_label_owner(self, module: str) -> list[str]:
        return [
            imported
            for imported in _imported_orchestrator_modules(module)
            if imported not in _ALLOWED_MODULES
        ]

    def _within_layers(self, imported: str, allowed_roots: tuple) -> bool:
        if imported in _ALLOWED_MODULES:
            return True
        return any(
            imported == root or imported.startswith(f"{root}.")
            for root in allowed_roots
        )


class PackageSurfaceTest(unittest.TestCase):
    """The package initializer carries no bindings of its own."""

    def test_initializer_exposes_no_owner_names(self) -> None:
        for owner_only_name in _OWNER_ONLY_NAMES:
            with self.subTest(name=owner_only_name), self.assertRaises(AttributeError):
                getattr(base_sync, owner_only_name)


class OwnerImportSiteTest(unittest.TestCase):
    """No module of this domain's own sits beside the owners."""

    def test_no_flat_module_exists(self) -> None:
        # Anything importable at these paths would be a second identity for the
        # pinned-state keys live issues are already parked on and the park
        # reasons the stage handlers short-circuit against -- free to drift
        # from the owner silently and invisible to a patch aimed at it.
        # Resolving the spec rather than stat-ing one path catches a copy
        # planted anywhere the interpreter would find it.
        for module in _FLAT_MODULES:
            with self.subTest(module=module):
                self.assertIsNone(find_spec(module))


if __name__ == "__main__":
    unittest.main()
