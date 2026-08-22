# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The branch a split leaves behind, retried where it can still be settled.

The umbrella's all-children-resolved branch is the last tick that could reclaim
it and the only one that comes back if it cannot, so these cases drive the real
stage handler: an issue that has become an umbrella never reaches the
transaction again.

"The branch" is every surface it exists on -- the remote ref, the checkout, and
the local ref -- because a remote delete beside a checkout that would not come
down leaves a worktree on a superseded branch that the per-tick base refresh
goes on merging into.
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
    SeededUmbrella,
    UMBRELLA,
    WORKFLOW_LOG,
    resource_states,
    split_umbrella,
    walk_umbrella,
)

# Four targets a ledger entry could name and this issue is not published under:
# an unprotected default branch, another issue's branch in the same namespace,
# one outside the namespace altogether, and -- the one a prefix-and-tail
# reading lets through -- another repository's branch for an issue whose number
# happens to match. Two specs sharing a `target_root` is what slug-namespacing
# exists for, so that last one is the ordinary shape, not a contrived one.
_MAIN = "main"

_ANOTHER_ISSUE = "orchestrator/geserdugarov__agent-orchestrator/issue-99"

_NOT_OURS = "feature/issue-41"

_ANOTHER_REPOSITORY = "orchestrator/other-repository/issue-41"


class _UmbrellaCleanupCase(_PatchedWorkflowMixin):
    """One umbrella tick over an issue that still owes a branch."""

    def _walk(self, owed: LateResourceState, **teardown) -> SeededUmbrella:
        """Seed an umbrella owing its branch that way, and run one tick."""
        seeded = split_umbrella(owed)
        walk_umbrella(self, seeded, **teardown)
        return seeded


class UmbrellaCleanupTest(_UmbrellaCleanupCase, unittest.TestCase):
    """An umbrella settles what it owes before it closes, or stays open."""

    def test_it_reclaims_the_owed_branch_and_closes(self) -> None:
        seeded = self._walk(LateResourceState.PENDING)

        self.assertEqual(
            seeded.github.deleted_remote_branches, [SUPERSEDED_BRANCH],
        )
        self.assertTrue(seeded.parent.closed)
        self.assertIn(RESOLVED_STAMP, seeded.github.pinned_data(PARENT_NUMBER))

    def test_it_records_what_the_reclamation_did(self) -> None:
        seeded = self._walk(LateResourceState.PENDING)

        self.assertEqual(
            resource_states(seeded.github), {SUPERSEDED_BRANCH: STATE_RECONCILED},
        )
        reported = [
            record for record in seeded.github.recorded_events
            if record.get("event") == EVENT_LATE_CLEANUP
        ]
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["outcome"], STATE_RECONCILED)

    def test_a_failed_obligation_is_retried(self) -> None:
        # "Recorded and retried" is the whole contract: the entry names the
        # branch so the retry asks about the same one.
        seeded = self._walk(LateResourceState.FAILED)

        self.assertEqual(
            seeded.github.deleted_remote_branches, [SUPERSEDED_BRANCH],
        )
        self.assertTrue(seeded.parent.closed)

    def test_a_settled_one_costs_no_second_call(self) -> None:
        seeded = self._walk(LateResourceState.RECONCILED)

        self.assertEqual(seeded.github.deleted_remote_branches, [])
        self.assertTrue(seeded.parent.closed)

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
        seeded = SeededUmbrella(github=github, parent=parent)

        walk_umbrella(self, seeded)

        self.assertEqual(seeded.github.deleted_remote_branches, [])
        self.assertTrue(seeded.parent.closed)


class UmbrellaCleanupRefusalTest(_UmbrellaCleanupCase, unittest.TestCase):
    """What a reclamation that did not finish costs the terminal."""

    def test_a_refusal_holds_the_umbrella_open(self) -> None:
        # Closing here would leave an obligation nobody ever settles: nothing
        # revisits a closed umbrella, and no other tick reads this ledger.
        seeded = split_umbrella(LateResourceState.PENDING)
        seeded.github._pull_state._delete_remote_branch_returns_ok = False

        with self.assertLogs(WORKFLOW_LOG, level="WARNING"):
            walk_umbrella(self, seeded)

        self.assertFalse(seeded.parent.closed)
        self.assertNotIn(RESOLVED_STAMP, seeded.github.pinned_data(PARENT_NUMBER))
        self.assertEqual(
            resource_states(seeded.github), {SUPERSEDED_BRANCH: STATE_FAILED},
        )

    def test_a_checkout_that_stays_holds_the_terminal(self) -> None:
        # A remote delete that succeeded beside a checkout that would not come
        # down is not a settled obligation.
        with self.assertLogs(WORKFLOW_LOG, level="WARNING"):
            seeded = self._walk(
                LateResourceState.PENDING, local_gone=False,
            )

        self.assertFalse(seeded.parent.closed)
        self.assertEqual(
            resource_states(seeded.github), {SUPERSEDED_BRANCH: STATE_FAILED},
        )

    def test_a_local_teardown_that_lands_later_closes(self) -> None:
        # "Recorded and retried" over the WHOLE ordinary cleanup, not just its
        # remote half: the entry stays owed until every surface is gone.
        seeded = split_umbrella(LateResourceState.PENDING)
        with self.assertLogs(WORKFLOW_LOG, level="WARNING"):
            walk_umbrella(self, seeded, local_gone=False)

        walk_umbrella(self, seeded)

        self.assertTrue(seeded.parent.closed)
        self.assertEqual(
            resource_states(seeded.github), {SUPERSEDED_BRANCH: STATE_RECONCILED},
        )

    def test_a_foreign_branch_is_never_deleted(self) -> None:
        # The target comes off a ledger a human can edit and is spent on a
        # destructive call, so a hand-edited entry naming an unprotected
        # branch must delete nothing -- and must not let the umbrella close
        # over an obligation nobody settled.
        for foreign in (_MAIN, _ANOTHER_ISSUE, _NOT_OURS, _ANOTHER_REPOSITORY):
            with self.subTest(branch=foreign):
                seeded = split_umbrella(
                    LateResourceState.PENDING, branch=foreign,
                )

                with self.assertLogs(WORKFLOW_LOG, level="ERROR"):
                    walk_umbrella(self, seeded)

                self.assertEqual(seeded.github.deleted_remote_branches, [])
                self.assertFalse(seeded.parent.closed)
                self.assertEqual(
                    resource_states(seeded.github), {foreign: STATE_FAILED},
                )


if __name__ == "__main__":
    unittest.main()
