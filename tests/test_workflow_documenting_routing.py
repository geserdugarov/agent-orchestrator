# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""`documenting` label bootstrap, family-aware partitioning, PR-refresh
detour membership, and dispatcher routing -- plus the end-to-end
no-`pr_number` park stability checks. Handler-behavior tests live in
`tests/test_workflow_documenting.py`."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import _TEST_SPEC


class DocumentingLabelRoutingTest(unittest.TestCase):
    """`documenting` is registered as a workflow label so the dispatcher
    routes it to the stub stage handler instead of falling through to
    pickup or implementation. The implementing stage does not auto-apply
    this label yet (parent #149), so any issue carrying it arrived via a
    manual operator action -- the stub parks awaiting human rather than
    silently skipping, otherwise the issue would sit forever waiting for a
    non-existent handler to advance it.
    """

    def test_documenting_label_is_recognized_as_workflow_label(self) -> None:
        from orchestrator.github import WORKFLOW_LABELS

        self.assertIn("documenting", WORKFLOW_LABELS)

    def test_documenting_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        self.assertIn("documenting", names)

    def test_documenting_label_sits_between_validating_and_in_review(
        self,
    ) -> None:
        # The happy-path lifecycle is implementing -> validating ->
        # documenting (final-docs hop) -> in_review; the spec tuple
        # places the labels in roughly that order so a reader scanning
        # WORKFLOW_LABEL_SPECS top-to-bottom sees the actual flow.
        # Lifecycle routing itself lives in the stage handlers, not
        # this tuple, but the order shouldn't actively mislead.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        impl_idx = names.index("implementing")
        val_idx = names.index("validating")
        doc_idx = names.index("documenting")
        in_review_idx = names.index("in_review")
        self.assertEqual(val_idx, impl_idx + 1)
        self.assertEqual(doc_idx, val_idx + 1)
        self.assertEqual(in_review_idx, doc_idx + 1)

    def test_documenting_label_is_not_family_aware(self) -> None:
        # Open `documenting` issues touch only their own pinned state and
        # worktree, so the label must stay out of `_FAMILY_AWARE_LABELS`
        # -- otherwise the parallel tick path would route it through the
        # single-threaded family bucket and defeat fan-out concurrency.
        self.assertNotIn("documenting", workflow._FAMILY_AWARE_LABELS)

    def test_documenting_label_is_in_pr_refresh_detour_set(self) -> None:
        # Behind-base PR-having worktrees need to be routed through
        # `resolving_conflict` by the pre-tick refresh. The brief final-
        # docs hop is PR-having (its sibling labels validating /
        # in_review / fixing already qualify), and the documenting
        # handler only checks ahead/behind vs. the PR branch -- not
        # base -- so without the detour a sibling-PR merge during the
        # docs pass would leave the docs commit on a stale base and
        # only the next in_review tick would catch it. Including the
        # label here is what keeps `hold_base_sync` as the only label
        # that gates auto-rebase for a PR-stage worktree.
        from orchestrator.worktrees import _PR_REFRESH_DETOUR_LABELS

        self.assertIn("documenting", _PR_REFRESH_DETOUR_LABELS)

    def test_dispatcher_routes_documenting_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(901, label="documenting")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_documenting") as handler, \
             patch.object(workflow, "_handle_pickup") as pickup, \
             patch.object(workflow, "_handle_implementing") as impl, \
             patch.object(workflow, "_handle_validating") as val:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)
        pickup.assert_not_called()
        impl.assert_not_called()
        val.assert_not_called()

    def test_documenting_without_pr_number_parks_awaiting_human(self) -> None:
        # End-to-end with the real handler: a manually-applied
        # `documenting` label on an issue with no pinned `pr_number`
        # cannot anchor on a dev PR worktree, so the handler parks
        # awaiting human rather than guessing.
        gh = FakeGitHubClient()
        issue = make_issue(902, label="documenting")
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(len(gh.posted_comments), 1)
        issue_number, body = gh.posted_comments[0]
        self.assertEqual(issue_number, 902)
        self.assertIn("documenting", body)
        self.assertTrue(gh.pinned_data(902).get("awaiting_human"))
        # The label is NOT flipped: parking surfaces the situation but
        # leaves the operator in control of the next move.
        self.assertEqual(gh.label_history, [])

    def test_documenting_missing_pr_number_is_idempotent_when_parked(
        self,
    ) -> None:
        # A second tick on an already-parked documenting issue (still
        # missing `pr_number`) must not re-post the parking comment or
        # re-emit the audit event -- otherwise every polling tick
        # would spam the issue.
        gh = FakeGitHubClient()
        issue = make_issue(903, label="documenting")
        gh.add_issue(issue)
        gh.seed_state(903, awaiting_human=True)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)


if __name__ == "__main__":
    unittest.main()
