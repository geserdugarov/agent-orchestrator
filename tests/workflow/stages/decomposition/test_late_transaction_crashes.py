# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Every boundary the split transaction can die at, and what it left behind.

Each case kills the process at one seam and then runs the transaction again
from what the pinned comment holds -- which is exactly what the next eligible
tick does, since the verdict is already recorded and the retry costs a read
rather than an agent. What is asserted is the pair the ordering exists for:
the durable fact that precedes the effect, and that the resume finishes
without repeating what already landed.
"""
from __future__ import annotations

import unittest

from orchestrator.git.snapshots import refs as _snapshot_refs
from orchestrator.workflow.stages.decomposition import (
    late_children as _late_children,
)
from orchestrator.workflow.stages.decomposition import (
    late_transaction as _late_transaction,
)
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateDisposition,
)
from orchestrator.workflow.state import WorkflowLabel

from tests.workflow.stages.decomposition.late_crash_support import (
    killed_after,
    killed_before,
)
from tests.workflow.stages.decomposition.late_test_support import (
    LATE_ISSUE_NUMBER,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    CHILDREN,
    KEY_CHILDREN,
    KEY_CONSUMERS,
    KEY_DECOMPOSED_AT,
    KEY_EXPECTED_CHILDREN,
    KEY_UMBRELLA,
    SNAPSHOT_REF,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    HeldPlanPrSplitCase,
    LateSplitCase,
    label_of,
)

RESOURCE_SNAPSHOT = "snapshot_ref"
RESOURCE_BRANCH = "branch"

STATE_PENDING = "pending"
STATE_RETAINED = "retained"
STATE_RECONCILED = "reconciled"


class SnapshotBoundaryTest(LateSplitCase, unittest.TestCase):
    """The intended ref is durable before anything is pushed."""

    def test_a_death_pre_push_leaves_the_intent(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_before(_snapshot_refs, "create_snapshot_ref"),
            )

        self.assertEqual(
            self._resources()[(RESOURCE_SNAPSHOT, SNAPSHOT_REF)],
            STATE_PENDING,
        )
        self.assertEqual(self.github.created_child_issues, [])

    def test_a_death_after_the_push_verifies(self) -> None:
        # The push landed and the write that would have recorded it did not:
        # create-or-verify is what makes the second attempt a read.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_after(_snapshot_refs, "create_snapshot_ref"),
            )

        resumed = self._resume()

        self.assertEqual(resumed.disposition, _LateDisposition.SETTLED)
        self.assertEqual(
            self._resources()[(RESOURCE_SNAPSHOT, SNAPSHOT_REF)],
            STATE_RETAINED,
        )


class ChildBoundaryTest(LateSplitCase, unittest.TestCase):
    """A child is recorded before anything else is done with it."""

    def test_a_death_pre_child_leaves_the_count(self) -> None:
        # What tells a partial split from a finished one, and what says the
        # parent has no implementation of its own to return to.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_before(self.github, "create_child_issue"),
            )

        pinned = self._pinned()
        self.assertEqual(pinned[KEY_EXPECTED_CHILDREN], len(CHILDREN))
        self.assertTrue(pinned[KEY_UMBRELLA])
        self.assertIsNone(pinned.get(KEY_CHILDREN))

    def test_a_death_mid_create_is_visible(self) -> None:
        # The one window the ordering accepts rather than closes: the create
        # returned and the parent never learned of it. The operator sees a
        # count the recorded children do not reach; nothing here silently
        # adopts an issue the parent cannot name.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_after(self.github, "create_child_issue"),
            )

        self.assertEqual(len(self.github.created_child_issues), 1)
        self.assertIsNone(self._pinned().get(KEY_CHILDREN))
        self.assertEqual(
            self._pinned()[KEY_EXPECTED_CHILDREN], len(CHILDREN),
        )

    def test_a_death_after_the_record_adopts(self) -> None:
        # Recorded first, so the retry reuses the child rather than opening a
        # second issue for the same slice -- and re-seeds it, since the seed
        # is the step that can have been lost.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(killed=killed_after(_late_children, "_recorded"))

        recorded = list(self._pinned()[KEY_CHILDREN])
        self.assertEqual(self._pinned()[KEY_CONSUMERS], recorded)

        resumed = self._resume()

        self.assertEqual(resumed.disposition, _LateDisposition.SETTLED)
        self.assertEqual(
            [child.number for child in self.github.created_child_issues],
            self._pinned()[KEY_CHILDREN],
        )
        self.assertEqual(len(self._pinned()[KEY_CHILDREN]), len(CHILDREN))


class AnnouncementBoundaryTest(LateSplitCase, unittest.TestCase):
    """The comment goes out ahead of the stamp that suppresses a repeat."""

    def test_a_death_pre_stamp_repeats_once(self) -> None:
        # The window costs a repeated sentence rather than an umbrella that
        # never said where its work went.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(killed=killed_after(self.github, "comment"))

        self.assertIsNone(self._pinned().get(KEY_DECOMPOSED_AT))

        self._resume()

        self.assertEqual(
            len([
                body for _, body in self.github.posted_comments
                if SNAPSHOT_REF in body
            ]),
            2,
        )
        self.assertIsNotNone(self._pinned()[KEY_DECOMPOSED_AT])


class SupersessionBoundaryTest(HeldPlanPrSplitCase, unittest.TestCase):
    """The pull request's own thread is what stops a repeated notice."""

    def test_a_death_post_notice_says_it_once(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                generation=self.generation,
                killed=killed_after(self.github, "supersede_pr"),
            )

        self._resume()

        self.assertEqual(
            len([
                body for _, body in self.github.posted_pr_comments
                if _late_transaction.SUPERSESSION_MARKER in body
            ]),
            1,
        )
        self.assertEqual(self.plan_pr.state, "closed")


class HandoffBoundaryTest(LateSplitCase, unittest.TestCase):
    """The parent is handed on before a child can run, and cleaned up after."""

    def test_a_death_pre_activation_is_umbrella(self) -> None:
        # A crash here cannot leave a runnable child under a parent still
        # labelled `decomposing`; the umbrella's own walk is the retry.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_before(_late_transaction._split,
                                     "_activate_initial_split_children"),
            )

        self.assertEqual(
            label_of(self.github, LATE_ISSUE_NUMBER), WorkflowLabel.UMBRELLA,
        )
        self.assertEqual(len(self._pinned()[KEY_CHILDREN]), len(CHILDREN))
        self.assertEqual(
            label_of(self.github, self.github.created_child_issues[0].number),
            WorkflowLabel.BLOCKED,
        )

    def test_a_death_pre_cleanup_leaves_it_owed(self) -> None:
        # The obligation is durable before the delete is attempted, so a
        # reclamation has something to retry rather than a gap.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_before(self.github, "delete_remote_branch"),
            )

        owed = [
            recorded for (kind, _), recorded in self._resources().items()
            if kind == RESOURCE_BRANCH
        ]
        self.assertEqual(owed, [STATE_PENDING])

    def test_a_death_post_delete_reconciles(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_after(self.github, "delete_remote_branch"),
            )

        self._resume()

        owed = [
            recorded for (kind, _), recorded in self._resources().items()
            if kind == RESOURCE_BRANCH
        ]
        self.assertEqual(owed, [STATE_RECONCILED])


if __name__ == "__main__":
    unittest.main()
