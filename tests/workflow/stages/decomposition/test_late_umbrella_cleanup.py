# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The branch a split leaves behind, retried where it can still be settled.

The umbrella's all-children-resolved branch is the last tick that could reclaim
it and the only one that comes back if it cannot, so these cases drive the real
stage handler: an issue that has become an umbrella never reaches the
transaction again.
"""
from __future__ import annotations

import unittest

from orchestrator.workflow.late_split.models import LateResourceState

from tests.support.fakes import FakeGitHubClient, make_issue
from tests.workflow.fixtures import _PatchedWorkflowMixin
from tests.workflow.stages.decomposition.late_cleanup_support import (
    CHILD_NUMBER,
    EVENT_LATE_CLEANUP,
    LABEL_DONE,
    PARENT_NUMBER,
    RESOLVED_STAMP,
    STATE_FAILED,
    STATE_RECONCILED,
)
from tests.workflow.stages.decomposition.late_cleanup_support import (
    SUPERSEDED_BRANCH,
    UMBRELLA,
    WORKFLOW_LOG,
    resource_states,
    split_umbrella,
    walk_umbrella,
)


class UmbrellaCleanupTest(_PatchedWorkflowMixin, unittest.TestCase):
    """An umbrella settles what it owes before it closes, or stays open."""

    def test_it_reclaims_the_owed_branch_and_closes(self) -> None:
        github, parent = split_umbrella(LateResourceState.PENDING)

        walk_umbrella(self, github, parent)

        self.assertEqual(
            github.deleted_remote_branches, [SUPERSEDED_BRANCH],
        )
        self.assertTrue(parent.closed)
        self.assertIn(RESOLVED_STAMP, github.pinned_data(PARENT_NUMBER))

    def test_it_records_what_the_reclamation_did(self) -> None:
        github, parent = split_umbrella(LateResourceState.PENDING)

        walk_umbrella(self, github, parent)

        self.assertEqual(
            resource_states(github), {SUPERSEDED_BRANCH: STATE_RECONCILED},
        )
        reported = [
            record for record in github.recorded_events
            if record.get("event") == EVENT_LATE_CLEANUP
        ]
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["outcome"], STATE_RECONCILED)

    def test_a_refusal_holds_the_umbrella_open(self) -> None:
        # Closing here would leave an obligation nobody ever settles: nothing
        # revisits a closed umbrella, and no other tick reads this ledger.
        github, parent = split_umbrella(LateResourceState.PENDING)
        github._pull_state._delete_remote_branch_returns_ok = False

        with self.assertLogs(WORKFLOW_LOG, level="WARNING"):
            walk_umbrella(self, github, parent)

        self.assertFalse(parent.closed)
        self.assertNotIn(RESOLVED_STAMP, github.pinned_data(PARENT_NUMBER))
        self.assertEqual(
            resource_states(github), {SUPERSEDED_BRANCH: STATE_FAILED},
        )

    def test_a_failed_obligation_is_retried(self) -> None:
        # "Recorded and retried" is the whole contract: the entry names the
        # branch so the retry asks about the same one.
        github, parent = split_umbrella(LateResourceState.FAILED)

        walk_umbrella(self, github, parent)

        self.assertEqual(
            github.deleted_remote_branches, [SUPERSEDED_BRANCH],
        )
        self.assertTrue(parent.closed)

    def test_a_settled_one_costs_no_second_call(self) -> None:
        github, parent = split_umbrella(LateResourceState.RECONCILED)

        walk_umbrella(self, github, parent)

        self.assertEqual(github.deleted_remote_branches, [])
        self.assertTrue(parent.closed)

    def test_an_umbrella_owing_nothing_is_left(self) -> None:
        # Every umbrella that reached its terminal another way answers the
        # same question without a write.
        github = FakeGitHubClient()
        parent = make_issue(PARENT_NUMBER, label=UMBRELLA)
        github.add_issue(parent)
        github.add_issue(make_issue(CHILD_NUMBER, label=LABEL_DONE))
        github.seed_state(
            PARENT_NUMBER, children=[CHILD_NUMBER], umbrella=True,
        )

        walk_umbrella(self, github, parent)

        self.assertEqual(github.deleted_remote_branches, [])
        self.assertTrue(parent.closed)


if __name__ == "__main__":
    unittest.main()
