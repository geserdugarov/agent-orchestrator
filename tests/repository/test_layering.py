# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The direction every import across the production tree runs in.

The tree is four layers deep and the dependency runs one way through them: the
configuration every domain reads sits at the bottom, the domains that do the
work sit above it, the workflow decides with them, and the launch forms compose
the lot. An import that points the other way is what turns a package into a
cycle, and what puts a decision the workflow owns behind an import of the
infrastructure under it.

The one name a lower layer may reach up for is the typed label vocabulary, and
only the two domains typed by it may: the GitHub and git layers are typed by
the workflow labels they read and write, and `workflow/state.py` is the one
owner holding those apart from the engine -- named exactly, so that a sibling
of it cannot inherit the exemption by wearing the same prefix. Because
importing a submodule runs the package initializer first, that is also why the
workflow's own initializer resolves the engine inside `tick` rather than binding
it -- an engine import there would send those layers back through the modules
they are still initializing.

The direction is read twice, because deferring an import weakens where it
lands but not whether it should be there. At module scope -- a class body
included, since that runs on the import too -- nothing may point up at all:
that is what decides whether a package can be loaded and what it costs. Over
every scope, the reaches that remain are the ones declared below, each a
base-sync owner reporting to the issue it runs for through the workflow's
comment and guard owners. A hop absent from that list fails wherever it is
written, and a hop on it fails if it is bound at module scope after all.
"""
from __future__ import annotations

import unittest
from types import MappingProxyType

from tests.repository.import_test_support import (
    every_import,
    module_scope_imports,
)
from tests.repository.layout_test_support import (
    PACKAGE,
    PACKAGE_ROOT,
    dotted_name,
    module_path,
    python_files,
)

# Bottom to top. A module may name its own layer and any layer below it.
_LAYERS = (
    ("config",),
    ("agents", "git", "github", "observability", "scheduler", "skills"),
    ("workflow",),
    ("__main__", "apps", "cli", "runtime"),
)

_TOP = len(_LAYERS) - 1

# The typed workflow state: the two label vocabularies, the transition graph
# keyed by them, and the guard every label write passes through.
_VOCABULARY = f"{PACKAGE}.workflow.state"

# The two domains typed by a workflow label: the GitHub layer reads and writes
# them, and the git layer carries them through a base sync. Nobody else has a
# reason to name anything under the workflow at import.
_VOCABULARY_READERS = (f"{PACKAGE}.git", f"{PACKAGE}.github")

_ENTRYPOINTS = tuple(f"{PACKAGE}.{domain}" for domain in _LAYERS[_TOP])

_BASE_SYNC = f"{PACKAGE}.git.base_sync"

_COMMENTS = f"{PACKAGE}.workflow.engine.comments"

_LATE_PUSH = f"{PACKAGE}.workflow.stages.implementing.late_push"

_LATE_RECORDS = f"{PACKAGE}.workflow.stages.implementing.late_records"

_LATE_REWRITE = f"{PACKAGE}.workflow.stages.implementing.late_rewrite"

_LATE_TRANSFER = f"{PACKAGE}.workflow.stages.implementing.late_transfer"

# The exemption a verdict left and the record that authorizes it to move: the
# two halves of the evidence a base sync assembles for the rewrite it is about
# to publish.
_EXEMPTION = f"{PACKAGE}.workflow.late_split.exemption"

_REWRITES = f"{PACKAGE}.workflow.late_split.rewrites"

_PUBLICATION = f"{PACKAGE}.git.publication"

# Every upward reach made inside a call, declared per module. A base sync runs
# under a git-layer owner but reports to the issue it was started for: the
# notice a rebase or a conflict posts goes out through the workflow's comment
# owner, the park a failed auto-rebase takes through its guard owner, and the
# rebase it is about to force-push is measured by the size gate first, since a
# base that moved changes what the branch adds to it and a pull request may
# not be grown past the ceiling by a refresh either. The squash on approval is
# the same argument without a measurement: it force-pushes onto a pull request
# the remote already carries, so it is entered on that publication before it
# rewrites anything and pushes through the gate's own call. All of them sit
# above this layer, so the import waits for the call that needs it -- at
# module scope it would be a cycle, since the workflow imports base sync back.
#
# The rebase is also a rewrite of whatever the branch stood on, so the same two
# owners reach the transfer seam: the publisher reads the exemption a verdict
# left and assembles the record that would authorize it to move, and the
# reset-and-park tail drops the permission its rollback will never spend.
_CALL_TIME_HOPS = MappingProxyType({
    f"{_BASE_SYNC}.conflicts": (_COMMENTS,),
    f"{_BASE_SYNC}.persistence": (
        _COMMENTS,
        f"{PACKAGE}.workflow.engine.guards",
        f"{PACKAGE}.workflow.stages.implementing.late_parks",
        _LATE_RECORDS,
        _LATE_TRANSFER,
    ),
    f"{_BASE_SYNC}.publication": (_COMMENTS, _LATE_PUSH, _LATE_RECORDS),
    f"{_BASE_SYNC}.transfers": (
        _EXEMPTION,
        f"{PACKAGE}.workflow.late_split.formats",
        f"{PACKAGE}.workflow.stages.implementing.late_overflow",
        f"{PACKAGE}.workflow.stages.implementing.late_parks",
        _LATE_RECORDS,
        f"{PACKAGE}.workflow.stages.implementing.late_rotation",
        _LATE_TRANSFER,
        _REWRITES,
    ),
    f"{_PUBLICATION}.rewrite": (_LATE_REWRITE,),
})


def _reads_the_vocabulary(module: str, target: str) -> bool:
    """Whether one import is the declared exception rather than a breach.

    Matched on the module boundary rather than on the prefix: a sibling named
    for the owner -- `workflow.stateful` -- is a different module, and reading
    it as the vocabulary would hand every future workflow owner the exemption
    by naming itself carefully.
    """
    named = target == _VOCABULARY or target.startswith(f"{_VOCABULARY}.")
    return named and module.startswith(_VOCABULARY_READERS)


def _layer_of(module: str) -> int:
    """Which layer a dotted module name belongs to.

    The root package itself belongs to the bottom: it is metadata, and every
    layer is free to name it.
    """
    domain = module.split(".")[1:2]
    for depth, layer in enumerate(_LAYERS):
        if domain and domain[0] in layer:
            return depth
    return 0


def _in_package(targets: frozenset[str]) -> tuple[str, ...]:
    """The import targets that land inside the orchestrator package."""
    return tuple(sorted(
        target for target in targets
        if target == PACKAGE or target.startswith(f"{PACKAGE}.")
    ))


def _points_up(module: str, target: str) -> bool:
    """Whether one import reaches a layer the module may not name."""
    if _reads_the_vocabulary(module, target):
        return False
    return _layer_of(target) > _layer_of(module)


class DependencyDirectionTest(unittest.TestCase):
    """Loading a module binds nothing from a layer above it."""

    def test_no_module_scope_import_points_up(self) -> None:
        # The label vocabulary read by the two domains typed by it is the
        # single declared exception, so the check doubles as the guard that the
        # exception stays one owner and two readers wide: an engine or stage
        # module bound below the workflow fails here, and so does a third
        # domain reaching for the vocabulary. The declared call-time hops fail
        # here too -- binding one at module scope is the cycle it is deferred
        # to avoid.
        for module in python_files(PACKAGE_ROOT):
            name = dotted_name(module, PACKAGE_ROOT)
            for target in _in_package(module_scope_imports(module)):
                with self.subTest(module=name, imported=target):
                    self.assertFalse(
                        _points_up(name, target),
                        f"{name} binds {target} from the layer above it",
                    )

    def test_only_declared_hops_reach_up_at_all(self) -> None:
        # A function body is not an escape hatch: an owner that reaches over
        # the layer above it inside a call still puts that layer's decisions in
        # its own, and the deferral only hides the edge from the import graph.
        # The hops that do exist are declared per module, so a new one is an
        # edit here with the reason beside it.
        for module in python_files(PACKAGE_ROOT):
            name = dotted_name(module, PACKAGE_ROOT)
            allowed = _CALL_TIME_HOPS.get(name, ())
            for target in _in_package(every_import(module)):
                with self.subTest(module=name, imported=target):
                    self.assertFalse(
                        _points_up(name, target) and target not in allowed,
                        f"{name} reaches {target} in the layer above it",
                    )

    def test_every_declared_hop_is_made(self) -> None:
        # The other direction: a hop left on the list after its call site is
        # gone is an exemption nothing needs, and the next upward reach from
        # that module would inherit it.
        for name, targets in _CALL_TIME_HOPS.items():
            reached = every_import(module_path(name, PACKAGE_ROOT))
            for target in targets:
                with self.subTest(module=name, imported=target):
                    self.assertIn(target, reached)

    def test_no_import_is_relative(self) -> None:
        # A relative import names its target by position rather than by owner,
        # so there is no layer to read off it and the check above would skip it
        # as though it left the package -- `from ..workflow import engine` is
        # the upward binding that would slip through. Every import in the tree
        # is absolute, which is also what makes the owner a name comes from
        # readable where it is written.
        for module in python_files(PACKAGE_ROOT):
            name = dotted_name(module, PACKAGE_ROOT)
            for target in sorted(every_import(module)):
                with self.subTest(module=name, imported=target):
                    self.assertFalse(target.startswith("."))

    def test_no_domain_module_imports_an_entrypoint(self) -> None:
        # The launch forms are what compose the domains, never what a domain
        # reads back -- at any scope, because a call-time hop into one would
        # drag the polling runtime, or Streamlit and Plotly behind a page, into
        # whatever reached for it.
        for module in python_files(PACKAGE_ROOT):
            name = dotted_name(module, PACKAGE_ROOT)
            if _layer_of(name) == _TOP:
                continue
            for target in _in_package(every_import(module)):
                with self.subTest(module=name, imported=target):
                    self.assertFalse(target.startswith(_ENTRYPOINTS))


if __name__ == "__main__":
    unittest.main()
