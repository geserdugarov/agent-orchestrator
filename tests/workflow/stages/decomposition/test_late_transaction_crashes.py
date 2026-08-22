# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a split leaves behind when it dies over its snapshot or its children.

The front half of the transaction: the ref every child is cut from, the
children themselves, and the sentence the parent owes. The back half -- the
supersession, the handoff, and the branch -- is the module beside this one.

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
from orchestrator.git.snapshots.refs import SnapshotOutcome
from orchestrator.workflow.stages.decomposition import (
    late_children as _late_children,
)
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateDisposition,
)

from tests.support.fakes import FakeComment, FakeUser
from tests.workflow.stages.decomposition.late_crash_support import (
    killed_after,
    killed_before,
)
from tests.workflow.stages.decomposition.late_seam_support import (
    SnapshotSeed,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    CHILDREN,
    FORWARD_LINK_MARKER,
    SNAPSHOT_REF,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    KEY_CHILDREN,
    KEY_CONSUMERS,
    KEY_DECOMPOSED_AT,
    KEY_EXPECTED_CHILDREN,
    KEY_LINKS_ANNOUNCED,
    KEY_UMBRELLA,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    LateSplitCase,
)

RESOURCE_SNAPSHOT = "snapshot_ref"

STATE_PENDING = "pending"

STATE_RETAINED = "retained"

EVENT_LATE_SNAPSHOT = "late_snapshot"


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

    def test_a_death_after_the_proof_retains(self) -> None:
        # The ref was created AND proved, and the write that would have moved
        # it from `pending` to `retained` never landed. The obligation is
        # already on the ledger, so nothing is lost and nothing is created.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_after(_snapshot_refs, "prove_snapshot_ref"),
            )

        self.assertEqual(
            self._resources()[(RESOURCE_SNAPSHOT, SNAPSHOT_REF)],
            STATE_PENDING,
        )
        self.assertEqual(self.github.created_child_issues, [])

        resumed = self._resume(
            snapshot=SnapshotSeed(create=SnapshotOutcome.PRESENT),
        )

        self.assertEqual(resumed.disposition, _LateDisposition.SETTLED)
        self.assertEqual(
            self._resources()[(RESOURCE_SNAPSHOT, SNAPSHOT_REF)],
            STATE_RETAINED,
        )
        self.assertEqual(
            len(self._events_named(EVENT_LATE_SNAPSHOT)), 1,
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

    def test_a_death_between_children_resumes(self) -> None:
        # The first slice is created, recorded, and seeded; the second has not
        # been touched. The resume adopts the first and opens only the second.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(killed=killed_after(_late_children, "_seeded"))
        first = list(self._pinned()[KEY_CHILDREN])
        self.assertEqual(len(first), 1)

        resumed = self._resume()

        self.assertEqual(resumed.disposition, _LateDisposition.SETTLED)
        self.assertEqual(len(self.github.created_child_issues), len(CHILDREN))
        self.assertEqual(self._pinned()[KEY_CHILDREN][:1], first)

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
    """The thread is what stops a repeat the durable receipt cannot."""

    def test_a_death_pre_stamp_says_it_once(self) -> None:
        # The comment landed and the write that recorded it did not, which is
        # indistinguishable from the outside -- so the resume looks for this
        # generation's own marker before saying anything.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(killed=killed_after(self.github, "comment"))

        self.assertFalse(self._pinned().get(KEY_LINKS_ANNOUNCED, False))

        self._resume()

        self.assertEqual(
            len([
                body for _, body in self.github.posted_comments
                if FORWARD_LINK_MARKER in body
            ]),
            1,
        )
        self.assertTrue(self._pinned()[KEY_LINKS_ANNOUNCED])
        self.assertIsNotNone(self._pinned()[KEY_DECOMPOSED_AT])

    def test_a_forged_marker_silences_nothing(self) -> None:
        # An HTML comment is invisible and trivially copied, so a third party
        # posting the marker must not suppress the one sentence saying where
        # this issue's work went.
        self.issue.comments.append(FakeComment(
            id=9, body=f"nothing to see\n\n{FORWARD_LINK_MARKER}",
            user=FakeUser("outsider"),
        ))

        self._transact()

        self.assertEqual(
            len([
                body for _, body in self.github.posted_comments
                if FORWARD_LINK_MARKER in body
            ]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
