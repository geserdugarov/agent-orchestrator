# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""When the snapshot a split preserved may finally be deleted.

The rule is the ref's own: every recorded direct consumer terminal, and the
umbrella's all-children-resolved branch is both the first moment that becomes
true for the children a split created and the last that could act on it.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from orchestrator.git.snapshots import refs as _snapshot_refs
from orchestrator.workflow.late_split.models import LateResourceState
from orchestrator.workflow.stages.decomposition import (
    late_cleanup as _late_cleanup,
)

from tests.workflow.fixtures import _PatchedWorkflowMixin
from tests.workflow.stages.decomposition.late_cleanup_support import (
    CHILD_NUMBER,
    LABEL_DONE,
    LABEL_IN_REVIEW,
    LABEL_REJECTED,
    PARENT_NUMBER,
    RecordedDelete,
    SNAPSHOT_REF,
)
from tests.workflow.stages.decomposition.late_cleanup_support import (
    STATE_FAILED,
    STATE_RECONCILED,
    STATE_RETAINED,
    WORKFLOW_LOG,
    resource_states,
    scan_of,
    split_umbrella,
    walk_umbrella,
)
from tests.workflow.stages.decomposition.late_test_support import (
    CANDIDATE_SHA,
    late_generation,
)

_OPAQUE_CONSUMERS = '["?"]'


class UmbrellaReclamationTest(_PatchedWorkflowMixin, unittest.TestCase):
    """A retained ref is deleted at the terminal, or holds it open."""

    def test_it_deletes_a_ref_its_consumers_left(self) -> None:
        github, parent = _retaining()

        deleted = self._walk_with(
            github, parent, _snapshot_refs.SnapshotOutcome.DELETED,
        )

        self.assertEqual(deleted.refs, [SNAPSHOT_REF])
        self.assertEqual(deleted.shas, [CANDIDATE_SHA])
        self.assertEqual(
            resource_states(github)[SNAPSHOT_REF], STATE_RECONCILED,
        )
        self.assertTrue(parent.closed)

    def test_a_repointed_ref_is_not_reclaimed(self) -> None:
        # Named against the commit the split preserved, so a ref somebody
        # re-pointed is refused rather than deleted -- and the refusal holds
        # the terminal open, because that is a human's to settle.
        github, parent = _retaining()

        with self.assertLogs(WORKFLOW_LOG, level="WARNING"):
            self._walk_with(
                github, parent, _snapshot_refs.SnapshotOutcome.MISMATCH,
            )

        self.assertEqual(resource_states(github)[SNAPSHOT_REF], STATE_FAILED)
        self.assertFalse(parent.closed)

    def test_an_absent_ref_is_already_reclaimed(self) -> None:
        # The crash between the push that deleted a ref and the write that
        # would have recorded it: absent is success, so the retry settles.
        github, parent = _retaining()

        self._walk_with(
            github, parent, _snapshot_refs.SnapshotOutcome.ABSENT,
        )

        self.assertEqual(
            resource_states(github)[SNAPSHOT_REF], STATE_RECONCILED,
        )
        self.assertTrue(parent.closed)

    def test_a_refused_delete_holds_the_terminal(self) -> None:
        # A permission or ruleset problem an operator has to see.
        github, parent = _retaining()

        with self.assertLogs(WORKFLOW_LOG, level="WARNING"):
            self._walk_with(
                github, parent, _snapshot_refs.SnapshotOutcome.REFUSED,
            )

        self.assertEqual(resource_states(github)[SNAPSHOT_REF], STATE_FAILED)
        self.assertFalse(parent.closed)

    def test_a_consumer_nobody_recorded_keeps_the_ref(self) -> None:
        # Fail-closed: a consumer the scan cannot speak for may still be
        # cutting from the ref -- and a retained ref does not block the close,
        # because nothing here can clear that condition.
        github, parent = _retaining()
        github.seed_state(
            PARENT_NUMBER,
            **{
                **github.pinned_data(PARENT_NUMBER),
                "late_consumers": [CHILD_NUMBER, CHILD_NUMBER + 5],
            },
        )

        deleted = self._walk_with(
            github, parent, _snapshot_refs.SnapshotOutcome.DELETED,
        )

        self.assertEqual(deleted.refs, [])
        self.assertEqual(
            resource_states(github)[SNAPSHOT_REF], STATE_RETAINED,
        )
        self.assertTrue(parent.closed)

    def test_a_death_post_delete_reconciles(self) -> None:
        # The delete landed and the write that recorded it did not. Absent is
        # success, so the retry asks once and settles the same entry rather
        # than reading a mismatch against a ref that is already gone.
        github, parent = _retaining()
        died = RecordedDelete(
            _snapshot_refs.SnapshotOutcome.DELETED, dies=True,
        )
        with self.assertRaises(KeyboardInterrupt):
            with patch.object(_snapshot_refs, "delete_snapshot_ref", died):
                walk_umbrella(self, github, parent)
        self.assertEqual(died.refs, [SNAPSHOT_REF])
        self.assertEqual(
            resource_states(github)[SNAPSHOT_REF], STATE_RETAINED,
        )
        self.assertFalse(parent.closed)

        self._walk_with(
            github, parent, _snapshot_refs.SnapshotOutcome.ABSENT,
        )

        self.assertEqual(
            resource_states(github)[SNAPSHOT_REF], STATE_RECONCILED,
        )
        self.assertTrue(parent.closed)

    def _walk_with(self, github, parent, outcome) -> RecordedDelete:
        """Run the umbrella tick with the remote answering `outcome`."""
        deleted = RecordedDelete(outcome)
        with patch.object(_snapshot_refs, "delete_snapshot_ref", deleted):
            walk_umbrella(self, github, parent)
        return deleted


class TerminalConsumerTest(unittest.TestCase):
    """Which dispositions prove a consumer will never cut from the ref again.

    Asked of the rule directly, because the umbrella's own terminal is only
    ever reached with every child `done`: the other two are what a consumer
    that ended WITHOUT publishing looks like, and the reclamation has to count
    them or a snapshot outlives every lineage one appears in.
    """

    def test_every_way_a_consumer_can_end_counts(self) -> None:
        for label, closed in (
            (LABEL_DONE, False), (LABEL_REJECTED, False), (None, True),
        ):
            with self.subTest(label=label, closed=closed):
                self.assertTrue(
                    _late_cleanup._reclaimable(
                        _one_consumer(), scan_of(label, closed=closed),
                    ),
                )

    def test_a_live_consumer_keeps_the_ref(self) -> None:
        for label in (LABEL_IN_REVIEW, None):
            with self.subTest(label=label):
                self.assertFalse(
                    _late_cleanup._reclaimable(
                        _one_consumer(), scan_of(label),
                    ),
                )

    def test_an_opaque_ledger_keeps_the_ref(self) -> None:
        # An entry this binary could not type is still a consumer, and not
        # one it can ask GitHub about.
        opaque = replace(_one_consumer(), opaque_consumers=_OPAQUE_CONSUMERS)

        self.assertFalse(
            _late_cleanup._reclaimable(opaque, scan_of(LABEL_DONE)),
        )

    def test_a_snapshot_with_no_consumers_is_kept(self) -> None:
        # Nothing recorded is not the same claim as nobody waiting.
        self.assertFalse(
            _late_cleanup._reclaimable(late_generation(), scan_of(LABEL_DONE)),
        )


def _retaining() -> tuple:
    """An umbrella whose branch is settled and whose ref is still held."""
    return split_umbrella(
        LateResourceState.RECONCILED, snapshot=LateResourceState.RETAINED,
    )


def _one_consumer():
    """A generation recording exactly the child the scan speaks for."""
    return late_generation().with_consumers((CHILD_NUMBER,))


if __name__ == "__main__":
    unittest.main()
