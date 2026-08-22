# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a split leaves behind when it dies over its snapshot or its links.

The ref every child is cut from, and the sentence the parent owes once they
exist. The children between them are the module beside this one, and the back
half -- the supersession, the handoff, and the branch -- is a third.

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
    FORWARD_LINK_MARKER,
    SNAPSHOT_REF,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    KEY_DECOMPOSED_AT,
    KEY_LINKS_ANNOUNCED,
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
