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
from orchestrator.workflow.stages.decomposition.models import _ChildScan

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

# What one obligation an older or newer binary recorded looks like: a kind
# this one cannot type, preserved verbatim rather than reduced to what it
# understood.
_UNTYPED_KIND = "unknown-to-this-binary"

_STATE_CLOSED = "closed"

_KIND_SNAPSHOT = "snapshot_ref"

# Three refs that are in the namespace, are shaped exactly like this one's, and
# belong to somebody else: another issue's, another cycle of this issue's, and
# another generation of this cycle's. Every one of them names the same commit,
# because a lineage is cut from one candidate.
_FOREIGN_REFS = (
    "refs/orchestrator/late-split/issue-99/cycle-3/gen-1",
    "refs/orchestrator/late-split/issue-41/cycle-4/gen-1",
    "refs/orchestrator/late-split/issue-41/cycle-3/gen-2",
)


class _RealShapedChild:
    """A closed consumer in the shape GitHub actually hands one back.

    A PyGithub issue carries `state` and nothing called `closed`, so the
    double's flag is the one spelling the reclamation never sees in
    production.
    """

    def __init__(self, number: int) -> None:
        self.number = number
        self.state = _STATE_CLOSED


class UmbrellaReclamationTest(_PatchedWorkflowMixin, unittest.TestCase):
    """A retained ref is deleted at the terminal, or holds it open."""

    def test_it_deletes_a_ref_its_consumers_left(self) -> None:
        seeded = _retaining()

        deleted = self._walk_with(
            seeded, _snapshot_refs.SnapshotOutcome.DELETED,
        )

        self.assertEqual(deleted.refs, [SNAPSHOT_REF])
        self.assertEqual(deleted.shas, [CANDIDATE_SHA])
        self.assertEqual(
            resource_states(seeded.github)[SNAPSHOT_REF], STATE_RECONCILED,
        )
        self.assertTrue(seeded.parent.closed)

    def test_a_repointed_ref_is_not_reclaimed(self) -> None:
        # Named against the commit the split preserved, so a ref somebody
        # re-pointed is refused rather than deleted -- and the refusal holds
        # the terminal open, because that is a human's to settle.
        seeded = _retaining()

        with self.assertLogs(WORKFLOW_LOG, level="WARNING"):
            self._walk_with(seeded, _snapshot_refs.SnapshotOutcome.MISMATCH)

        self.assertEqual(resource_states(seeded.github)[SNAPSHOT_REF], STATE_FAILED)
        self.assertFalse(seeded.parent.closed)

    def test_an_absent_ref_is_already_reclaimed(self) -> None:
        # The crash between the push that deleted a ref and the write that
        # would have recorded it: absent is success, so the retry settles.
        seeded = _retaining()

        self._walk_with(
            seeded, _snapshot_refs.SnapshotOutcome.ABSENT,
        )

        self.assertEqual(
            resource_states(seeded.github)[SNAPSHOT_REF], STATE_RECONCILED,
        )
        self.assertTrue(seeded.parent.closed)

    def test_a_refused_delete_holds_the_terminal(self) -> None:
        # A permission or ruleset problem an operator has to see.
        seeded = _retaining()

        with self.assertLogs(WORKFLOW_LOG, level="WARNING"):
            self._walk_with(seeded, _snapshot_refs.SnapshotOutcome.REFUSED)

        self.assertEqual(resource_states(seeded.github)[SNAPSHOT_REF], STATE_FAILED)
        self.assertFalse(seeded.parent.closed)

    def test_a_consumer_nobody_recorded_keeps_the_ref(self) -> None:
        # Fail-closed: a consumer the scan cannot speak for may still be
        # cutting from the ref -- and a retained ref does not block the close,
        # because nothing here can clear that condition.
        seeded = _retaining()
        seeded.github.seed_state(
            PARENT_NUMBER,
            **{
                **seeded.github.pinned_data(PARENT_NUMBER),
                "late_consumers": [CHILD_NUMBER, CHILD_NUMBER + 5],
            },
        )

        deleted = self._walk_with(
            seeded, _snapshot_refs.SnapshotOutcome.DELETED,
        )

        self.assertEqual(deleted.refs, [])
        self.assertEqual(
            resource_states(seeded.github)[SNAPSHOT_REF], STATE_RETAINED,
        )
        self.assertTrue(seeded.parent.closed)

    def test_a_death_post_delete_reconciles(self) -> None:
        # The delete landed and the write that recorded it did not. Absent is
        # success, so the retry asks once and settles the same entry rather
        # than reading a mismatch against a ref that is already gone.
        seeded = _retaining()
        died = RecordedDelete(
            _snapshot_refs.SnapshotOutcome.DELETED, dies=True,
        )
        with self.assertRaises(KeyboardInterrupt):
            with patch.object(_snapshot_refs, "delete_snapshot_ref", died):
                walk_umbrella(self, seeded)
        self.assertEqual(died.refs, [SNAPSHOT_REF])
        self.assertEqual(
            resource_states(seeded.github)[SNAPSHOT_REF], STATE_RETAINED,
        )
        self.assertFalse(seeded.parent.closed)

        self._walk_with(
            seeded, _snapshot_refs.SnapshotOutcome.ABSENT,
        )

        self.assertEqual(
            resource_states(seeded.github)[SNAPSHOT_REF], STATE_RECONCILED,
        )
        self.assertTrue(seeded.parent.closed)

    def _walk_with(self, seeded, outcome) -> RecordedDelete:
        """Run the umbrella tick with the remote answering `outcome`."""
        deleted = RecordedDelete(outcome)
        with patch.object(_snapshot_refs, "delete_snapshot_ref", deleted):
            walk_umbrella(self, seeded)
        return deleted


class UnprovableObligationTest(_PatchedWorkflowMixin, unittest.TestCase):
    """Nothing unreadable closes an umbrella, and nothing foreign is deleted.

    Two halves of the same discipline: an obligation this binary cannot read
    holds the terminal open, and a target it cannot prove is this issue's own
    is refused before the remote is touched -- which then holds the terminal
    open too, because a refusal is still an obligation.
    """

    def test_an_opaque_ledger_holds_the_terminal(self) -> None:
        # The entries it could not type are still obligations, and the typed
        # ones beside them are not the whole of what is owed -- so closing on
        # the strength of that projection is the reading the verbatim copy
        # exists to prevent.
        seeded = split_umbrella(LateResourceState.RECONCILED)
        _seed_resources(seeded.github, [{"kind": _UNTYPED_KIND}])

        walk_umbrella(self, seeded)

        self.assertFalse(seeded.parent.closed)
        self.assertEqual(seeded.github.deleted_remote_branches, [])

    def test_a_foreign_identity_is_never_deleted(self) -> None:
        # The transport proves the namespace and the commit, and neither is
        # identity: every generation in a lineage was cut from one candidate
        # and names the same SHA, so a hand-edited entry pointing at a
        # sibling's ref would pass both tests and destroy the only copy of
        # exactly what that sibling was told to reuse.
        for foreign in _FOREIGN_REFS:
            with self.subTest(ref=foreign):
                seeded = _retaining()
                github = seeded.github
                _seed_resources(github, [{
                    "kind": _KIND_SNAPSHOT,
                    "target": foreign,
                    "state": STATE_RETAINED,
                }])
                deleted = RecordedDelete(
                    _snapshot_refs.SnapshotOutcome.DELETED,
                )

                held = patch.object(
                    _snapshot_refs, "delete_snapshot_ref", deleted,
                )
                with self.assertLogs(WORKFLOW_LOG, level="ERROR"), held:
                    walk_umbrella(self, seeded)

                self.assertEqual(deleted.refs, [])
                self.assertEqual(resource_states(github)[foreign], STATE_FAILED)
                self.assertFalse(seeded.parent.closed)

    def test_a_damaged_identity_holds_the_terminal(self) -> None:
        # A record whose cycle identity cannot be read still writes what it
        # owes; there is just nothing to correlate a reclamation to and no
        # issue number to prove a branch belongs to this generation.
        seeded = split_umbrella(LateResourceState.PENDING)
        _seed_resources(seeded.github, damaged=True)

        with self.assertLogs(WORKFLOW_LOG, level="ERROR"):
            walk_umbrella(self, seeded)

        self.assertFalse(seeded.parent.closed)
        self.assertEqual(seeded.github.deleted_remote_branches, [])

    def test_a_damaged_identity_owing_nothing_closes(self) -> None:
        # Every umbrella the initial decomposer made carries no ledger at all,
        # and answers without a write.
        seeded = split_umbrella(LateResourceState.PENDING)
        _seed_resources(seeded.github, damaged=True, resources=None)

        walk_umbrella(self, seeded)

        self.assertTrue(seeded.parent.closed)


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

    def test_a_real_shaped_close_counts(self) -> None:
        # The close is the whole answer here, since the label is one a running
        # child wears. Asked for the double's flag instead, this consumer
        # reads as live and the ref it holds is never reclaimed.
        scan = _ChildScan(
            children=[CHILD_NUMBER],
            issues={CHILD_NUMBER: _RealShapedChild(CHILD_NUMBER)},
            labels={CHILD_NUMBER: LABEL_IN_REVIEW},
        )

        self.assertTrue(_late_cleanup._reclaimable(_one_consumer(), scan))

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


def _seed_resources(github, resources=(), *, damaged: bool = False) -> None:
    """Re-seed the parent's ledger, optionally without a readable identity."""
    pinned = dict(github.pinned_data(PARENT_NUMBER))
    if resources is None:
        pinned.pop("late_resources", None)
    elif resources:
        pinned["late_resources"] = resources
    if damaged:
        pinned.pop("late_cycle_id", None)
    github.seed_state(PARENT_NUMBER, **pinned)


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
