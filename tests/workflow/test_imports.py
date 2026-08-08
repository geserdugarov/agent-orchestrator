# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Clean-process import checks and package surface for the workflow package."""

from __future__ import annotations

import importlib
import subprocess
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from types import MappingProxyType

from orchestrator import _workflow_export_manifest
from orchestrator import workflow as _workflow
from orchestrator.workflow import engine as _engine
from tests.reexport_test_support import lazy_targets, resolve_target

_ENGINE_OWNERS = (
    "comments",
    "dispatch",
    "drift",
    "guards",
    "messages",
    "pickup",
    "prompts",
    "terminals",
    "tick",
    "usage",
)

_MODULES = (
    "orchestrator.workflow",
    "orchestrator.workflow.engine",
    *(f"orchestrator.workflow.engine.{owner}" for owner in _ENGINE_OWNERS),
    "orchestrator.workflow.state",
)

# Manifest targets, what they resolve to, and the two subpackages beside the
# facade, so importing it must leave every one of them out of `sys.modules`: the
# dispatcher, the tick loop, the stage-handler tree, the git and GitHub
# subsystems those reach, and the analytics and config packages behind the
# shared dependency bindings.
_DEFERRED_MODULES = (
    "orchestrator.analytics",
    "orchestrator.config",
    "orchestrator.git",
    "orchestrator.github",
    "orchestrator.workflow.engine",
    "orchestrator.workflow.engine.dispatch",
    "orchestrator.workflow.engine.tick",
    "orchestrator.workflow.stages",
)

# The `state` owner is what the GitHub and git layers below the engine are typed
# by, so an import of it has to cost no more than the initializer it runs.
_LAZY_IMPORTS = (
    "orchestrator.workflow",
    "orchestrator.workflow.state",
)

_LAZINESS_PROBE = (
    "import sys;"
    "import {module};"
    "print(' '.join(name for name in {names!r} if name in sys.modules))"
)

# One export per resolver branch: a stage handler the manifest reads off its
# leaf, and a whole module the manifest binds by name.
_PROBE_EXPORTS = ("_handle_ready", "contextlib")

# The module paths a second import site for the drift owner, or for the comment,
# message, prompt, and decomposition manifest owners one flat spelling would
# answer for together, would take: the flat spelling itself, and the inventory
# and resolver hooks one would be built from.
_FLAT_MODULES = (
    "orchestrator._workflow_drift_export_manifest",
    "orchestrator._workflow_drift_exports",
    "orchestrator._workflow_messages_export_manifest",
    "orchestrator._workflow_messages_exports",
    "orchestrator.workflow_drift",
    "orchestrator.workflow_messages",
)

_GIT_PREFIX = "orchestrator.git."

# Every git name the facade publishes, keyed by the owner that defines it: the
# token-carrying fetches and push, the base-sync rebases and the vocabulary
# they park by, the plain and hardened runners, the subject probes and title
# helpers behind a PR title, the squash entry point, the HEAD and dirty reads,
# and the worktree creation, naming, recovery, decomposition, and teardown
# helpers. Every one is published for callers outside the tree: the stage side
# names the owner, so what this pins is the forward, not a live read.
_GIT_PUBLISHED = MappingProxyType({
    "orchestrator.git.authentication": (
        "_authed_fetch",
        "_authed_target_fetch",
        "_push_branch",
    ),
    "orchestrator.git.base_sync.pr": ("_sync_pr_worktree_to_base",),
    "orchestrator.git.base_sync.pre_pr": (
        "_merge_base_into_worktree",
        "_rebase_base_into_worktree",
        "_rebase_in_progress",
    ),
    "orchestrator.git.base_sync.refresh": (
        "_refresh_base_and_worktrees",
        "_sync_worktree_with_base",
    ),
    "orchestrator.git.base_sync.state": ("_AUTO_REBASE_PARK_REASONS",),
    "orchestrator.git.commands": ("_git", "_git_hardened"),
    "orchestrator.git.publication.probes": (
        "_branch_ahead_behind",
        "_first_commit_subject",
        "_is_conventional_subject",
        "_is_prefixed_subject",
    ),
    "orchestrator.git.publication.squash": ("_squash_and_force_push",),
    "orchestrator.git.publication.titles": (
        "_infer_subject_prefix",
        "_pr_title_from_commit_or_issue",
    ),
    "orchestrator.git.verification.probes": (
        "_head_sha",
        "_worktree_dirty_files",
    ),
    "orchestrator.git.worktrees.creation": (
        "_ensure_pr_worktree",
        "_ensure_worktree",
        "_has_new_commits",
    ),
    "orchestrator.git.worktrees.decomposition": (
        "_cleanup_decompose_worktree",
        "_decompose_worktree_path",
        "_ensure_decompose_worktree",
    ),
    "orchestrator.git.worktrees.paths": (
        "_branch_name",
        "_resolve_branch_name",
        "_sanitize_branch_segment",
        "_sanitize_slug",
        "_worktree_path",
    ),
    "orchestrator.git.worktrees.recovery": ("_branch_has_unpushed_commits",),
    "orchestrator.git.worktrees.terminal": (
        "_cleanup_question_worktree",
        "_cleanup_terminal_branch",
    ),
})


class CleanProcessImportTest(unittest.TestCase):
    """The package, its subpackage, and each owner beneath them import alone.

    The initializer installs hooks that resolve the export manifest, and the
    leaves those hooks reach import `orchestrator.workflow` back at call time.
    A subprocess per module gives each a clean `sys.modules` no other test has
    already populated, exposing an import-order cycle a package-first suite run
    would mask.
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

    def test_import_resolves_no_target(self) -> None:
        # The package boundary is where an accidental eager binding is cheapest
        # to add and hardest to notice: a submodule or dependency import in the
        # initializer would drag the stage tree or the analytics graph into every
        # `orchestrator.workflow` import -- and into the GitHub and git layers
        # that import the state owner beside it -- which the flat suite could
        # never observe.
        for module in _LAZY_IMPORTS:
            with self.subTest(module=module):
                self._assert_nothing_resolved(module)

    def _assert_nothing_resolved(self, module: str) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _LAZINESS_PROBE.format(
                    module=module, names=_DEFERRED_MODULES,
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertEqual(completed.stdout.strip(), "")


class PackageSurfaceTest(unittest.TestCase):
    """The initializer is the facade; the engine subpackage owns no names."""

    def test_facade_lives_in_the_package_initializer(self) -> None:
        # The manifest keys its resolver on `orchestrator.workflow` and the
        # `.pyi` surface is matched against that module's own path, so the
        # facade has to stay the initializer rather than sit in a leaf the
        # package re-exports.
        self.assertTrue(hasattr(_workflow, "__path__"))
        initializer = Path(_workflow.__file__)
        self.assertEqual(initializer.name, "__init__.py")
        self.assertEqual(initializer.parent.name, "workflow")
        self.assertTrue(initializer.with_suffix(".pyi").is_file())

    def test_engine_initializer_binds_nothing(self) -> None:
        # Importing an owner plants it in the package namespace, so a submodule
        # is the only thing allowed to appear here. A re-export beside it would
        # make the initializer a second identity for that owner and charge every
        # importer of one owner for the imports of all the others.
        for owner in _ENGINE_OWNERS:
            with self.subTest(owner=owner):
                self.assertIs(
                    getattr(_engine, owner),
                    importlib.import_module(f"{_engine.__name__}.{owner}"),
                )
        for name, bound in _engine.__dict__.items():
            if name.startswith("__"):
                continue
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(bound, "__name__", None), f"{_engine.__name__}.{name}",
                )

    def test_submodule_keeps_lazy_hooks(self) -> None:
        # Importing a submodule plants it in the package namespace the hooks
        # cache resolved exports into, so the two share one dict.
        self.assertIs(_workflow.engine, _engine)
        targets = lazy_targets(_workflow_export_manifest)
        for export_name in _PROBE_EXPORTS:
            with self.subTest(name=export_name):
                resolved = resolve_target(
                    _workflow, export_name, targets[export_name],
                )
                self.assertIs(resolved.direct, resolved.expected)
                self.assertIs(resolved.imported, resolved.expected)
        self.assertEqual(
            _workflow.__all__, _workflow_export_manifest.EXPORTED_NAMES,
        )
        self.assertIn("engine", _workflow.__dir__())


class GitInventoryTest(unittest.TestCase):
    """The facade resolves every git name off the owner that defines it."""

    def test_each_name_declares_its_owner(self) -> None:
        # A forwarder of an owner hands back the same object, so identity
        # alone cannot say which module the facade reads a name off. The
        # declared target is what separates the two -- and only naming the
        # owner keeps a patch aimed at the owner and one aimed at the facade
        # two interceptions rather than three.
        targets = lazy_targets(_workflow_export_manifest)
        for owner_name, export_names in _GIT_PUBLISHED.items():
            owner = importlib.import_module(owner_name)
            for export_name in export_names:
                with self.subTest(name=export_name):
                    self.assertEqual(
                        targets[export_name].module_name, owner_name,
                    )
                    self.assertIs(
                        getattr(_workflow, export_name),
                        getattr(owner, export_name),
                    )

    def test_the_git_slice_is_the_declared_one(self) -> None:
        # Comparing both directions keeps the table above the whole git slice,
        # so a name routed to a new owner -- or one added to the inventory --
        # is an edit here rather than a target nothing checks.
        self.assertEqual(
            {
                target.export_name
                for target in _workflow_export_manifest.EXPORTS
                if target.module_name.startswith(_GIT_PREFIX)
            },
            {
                export_name
                for export_names in _GIT_PUBLISHED.values()
                for export_name in export_names
            },
        )


class OwnerImportSiteTest(unittest.TestCase):
    """The engine owners are the only modules their surfaces answer on."""

    def test_no_flat_module_exists(self) -> None:
        # Anything importable at these paths would be a second identity for the
        # hash live issues are already parked on, the marker their comments are
        # stamped with, or the prompt text an agent is spawned with -- free to
        # drift from the owner silently and invisible to a patch aimed at it.
        # Resolving the spec rather than stat-ing one path catches a copy
        # planted anywhere the interpreter would find it.
        for module in _FLAT_MODULES:
            with self.subTest(module=module):
                self.assertIsNone(find_spec(module))


if __name__ == "__main__":
    unittest.main()
