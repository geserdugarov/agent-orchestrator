# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing routing behavior."""

from __future__ import annotations

import unittest

from orchestrator import github, worktrees
from tests import fixing_routing_test_support as support
from tests.implementing_fixing_test_cases import IssueScenario

DISPATCH_ISSUE = support.DISPATCH_ISSUE
FakeGitHubClient = support.FakeGitHubClient
LABEL_FIXING = support.LABEL_FIXING
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_TEST_SPEC = support._TEST_SPEC
make_issue = support.make_issue
patch = support.patch
workflow = support.workflow


class FixingLabelDefinitionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`fixing` is registered as a workflow label that sits between
    `in_review` and `validating` in the PR-feedback fix loop. The dispatcher
    must route the label to `_handle_fixing` instead of falling through to
    pickup or implementation, and the bootstrap specs / family-aware
    partitioning / closed-issue sweep / PR-worktree refresh detour must
    all recognise it as a PR-having stage. The PR-terminal arcs and the
    no-`pr_number` park covered here pair with the quiet-window / dev-
    resume tests in `tests/test_workflow_fixing.py`.
    """

    def test_label_is_recognized(self) -> None:
        self.assertIn(LABEL_FIXING, github.WORKFLOW_LABELS)

    def test_fixing_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        names = [name for name, _, _ in github.WORKFLOW_LABEL_SPECS]
        self.assertIn(LABEL_FIXING, names)

    def test_label_between_review_and_conflict(
        self,
    ) -> None:
        # Lifecycle order matters: `fixing` is the next stage after
        # `in_review` when the PR has fresh feedback. The spec tuple
        # encodes the lifecycle ordering, so it must place `fixing` right
        # after `in_review`.
        names = [name for name, _, _ in github.WORKFLOW_LABEL_SPECS]
        in_review_idx = names.index("in_review")
        fixing_idx = names.index(LABEL_FIXING)
        self.assertEqual(fixing_idx, in_review_idx + 1)

    def test_fixing_label_is_not_family_aware(self) -> None:
        # Open `fixing` issues touch only their own pinned state and PR
        # worktree, so the label must stay out of `_FAMILY_AWARE_LABELS` --
        # otherwise the parallel tick path would route it through the
        # single-threaded family bucket and defeat fan-out concurrency.
        self.assertNotIn(LABEL_FIXING, workflow._FAMILY_AWARE_LABELS)

    def test_fixing_label_is_in_pr_refresh_detour_set(self) -> None:
        # Behind-base PR-having worktrees need to be routed through
        # `resolving_conflict` by the pre-tick refresh; a `fixing` worktree
        # is PR-having (its sibling labels validating/in_review already
        # qualify) so it must be eligible for the same detour.
        self.assertIn(LABEL_FIXING, worktrees._PR_REFRESH_DETOUR_LABELS)

    def test_dispatcher_routes_fixing_to_handler(self) -> None:
        scenario = IssueScenario(
            FakeGitHubClient(),
            make_issue(DISPATCH_ISSUE, label=LABEL_FIXING),
        )
        scenario.github.add_issue(scenario.issue)

        with (
            patch.object(workflow, "_handle_fixing") as fixing_handler,
            patch.object(workflow, "_handle_pickup") as pickup,
            patch.object(workflow, "_handle_implementing") as impl,
            patch.object(workflow, "_handle_in_review") as in_review,
        ):
            workflow._process_issue(
                scenario.github,
                _TEST_SPEC,
                scenario.issue,
            )
            fixing_handler.assert_called_once_with(
                scenario.github,
                _TEST_SPEC,
                scenario.issue,
            )
            pickup.assert_not_called()
            impl.assert_not_called()
            in_review.assert_not_called()
