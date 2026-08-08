# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The tick owner's per-tick pass order, and the facade still forwarding it."""
from __future__ import annotations

import functools
import unittest
from unittest.mock import patch

from orchestrator import _workflow_export_manifest, workflow
from orchestrator.skills import catalog
from orchestrator.workflow.engine import dispatch, tick

from tests.fakes import FakeGitHubClient
from tests.reexport_test_support import lazy_targets
from tests.workflow_git_owners import seam_patch
from tests.workflow_repo_values import _TEST_SPEC


# Every name the workflow facade answers for, and nothing besides. `tick`
# itself is the entry point `main._run_tick` calls per repo, so a forward that
# stops resolving takes the polling loop with it -- and a name added to the
# inventory is a new public export that outlives whatever refactor introduced
# it.
_FACADE_FORWARDS = (
    "_CommunityContribution",
    "_ParallelTickPlan",
    "_community_contribution_for_pr",
    "_drain_family_bucket",
    "_drain_parallel_futures",
    "_label_community_contribution",
    "_run_parallel_tick",
    "_run_sequential_tick",
    "_sweep_community_contribution_prs",
    "_sweep_pr_contribution",
    "tick",
)

_EXPECTED_PASSES = ("refresh", "sweep", "catalog", "dispatch")

_REFRESH_BASE = "_refresh_base_and_worktrees"


class _PassRecorder:
    """Stands in for one tick pass and notes when it fired."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def pass_named(self, name: str):
        return functools.partial(self._record, name)

    def _record(self, name: str, *_args, **_kwargs) -> None:
        self.calls.append(name)


class TickPassOrderTest(unittest.TestCase):
    """The passes run once each, in the order the later ones depend on."""

    def test_pass_order_holds_on_both_routes(self) -> None:
        # The base fetch has to land before the two passes that read what it
        # left behind -- a handler would otherwise rebase onto the SHA its
        # worktree was created at, and the catalog would ls-tree a stale base
        # ref -- and the sweep and the catalog have to sit before the
        # scheduler / in-tick split rather than inside one branch, or a
        # scheduler-driven deployment silently stops labeling outsider PRs and
        # reporting its skill catalog.
        for scheduler in (None, object()):
            with self.subTest(scheduler=scheduler is not None):
                self.assertEqual(
                    self._passes_driven_by(scheduler), list(_EXPECTED_PASSES),
                )

    def _passes_driven_by(self, scheduler) -> list[str]:
        recorder = _PassRecorder()
        with (
            seam_patch(_REFRESH_BASE, recorder.pass_named("refresh")),
            patch.object(
                tick, "_sweep_community_contribution_prs",
                recorder.pass_named("sweep"),
            ),
            patch.object(
                catalog, "_emit_repo_skill_catalog",
                recorder.pass_named("catalog"),
            ),
            patch.object(
                dispatch, "_dispatch_via_scheduler",
                recorder.pass_named("dispatch"),
            ),
            patch.object(
                tick, "_run_sequential_tick", recorder.pass_named("dispatch"),
            ),
        ):
            tick.tick(FakeGitHubClient(), _TEST_SPEC, scheduler=scheduler)
        return recorder.calls


class TickFacadeForwardTest(unittest.TestCase):
    """The workflow facade resolves each name to the owner's exact object."""

    def test_facade_forwards_the_owner_objects(self) -> None:
        for forwarded_name in _FACADE_FORWARDS:
            with self.subTest(name=forwarded_name):
                self.assertIs(
                    getattr(workflow, forwarded_name),
                    getattr(tick, forwarded_name),
                )

    def test_facade_inventory_is_the_historical_names(self) -> None:
        # The manifest is the compatibility surface, not a mirror of the owner:
        # a helper introduced while the owner is assembled must not become a
        # facade export on the way in. Comparing both directions is what keeps
        # an addition as visible as a dropped forward.
        self.assertEqual(
            {
                export_name
                for export_name, target in lazy_targets(_workflow_export_manifest).items()
                if target.module_name == tick.__name__
            },
            set(_FACADE_FORWARDS),
        )


if __name__ == "__main__":
    unittest.main()
