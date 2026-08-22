# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The immutable copy a split is cut from, and what its absence costs."""
from __future__ import annotations

import unittest
from dataclasses import replace

from orchestrator.git.snapshots.refs import SnapshotOutcome
from orchestrator.workflow.stages.decomposition import (
    late_snapshot as _late_snapshot,
)
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateDisposition,
)

from tests.workflow.stages.decomposition.late_test_support import (
    CANDIDATE_SHA,
    KEYS,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    EVENT_LATE_FAILURE,
    EVENT_LATE_SNAPSHOT,
    KEY_CHILDREN,
    PARK_CHILDREN_FAILED,
    PARK_SNAPSHOT_FAILED,
    SNAPSHOT_REF,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    LateSplitCase,
    refused_snapshot,
    unproven_snapshot,
)

RESOURCE_SNAPSHOT = "snapshot_ref"

# An obligation an older or newer binary recorded, kept verbatim because this
# one cannot type it.
OPAQUE_LEDGER = '[{"kind": "unknown-to-this-binary"}]'

STATE_RETAINED = "retained"
STATE_FAILED = "failed"

FAILURE_SNAPSHOT = "snapshot_failed"

# Every way the remote can refuse to establish a snapshot, and whether the
# refusal was about creating it or about serving it back. Each has to end the
# same way: nothing created, the obligation recorded as failed, and the issue
# parked with the verdict still standing.
REFUSALS = (
    ("a ref another commit occupies", refused_snapshot(
        SnapshotOutcome.MISMATCH,
    )),
    ("a remote nobody could ask", refused_snapshot(
        SnapshotOutcome.UNREADABLE,
    )),
    ("a namespace the token cannot write", refused_snapshot(
        SnapshotOutcome.REFUSED,
    )),
    ("a ref that would not fetch back", unproven_snapshot(
        SnapshotOutcome.REFUSED,
    )),
    ("a fetch that brought another commit", unproven_snapshot(
        SnapshotOutcome.MISMATCH,
    )),
)


class SnapshotEstablishedTest(LateSplitCase, unittest.TestCase):
    """A proved snapshot is recorded as an obligation before any child."""

    def test_it_records_the_ref_the_identity_names(self) -> None:
        self._transact()

        self.assertEqual(
            self._resources()[(RESOURCE_SNAPSHOT, SNAPSHOT_REF)],
            STATE_RETAINED,
        )

    def test_a_proved_snapshot_reaches_both_sinks(self) -> None:
        self._transact()

        reported = self._events_named(EVENT_LATE_SNAPSHOT)

        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["outcome"], STATE_RETAINED)

    def test_an_existing_ref_is_not_rewritten(self) -> None:
        # The create-or-verify answer a crashed tick relies on: the ref it
        # already pushed is the answer and the transaction carries on.
        outcome = self._transact(
            snapshot=refused_snapshot(SnapshotOutcome.PRESENT),
        )

        self.assertEqual(outcome.disposition, _LateDisposition.SETTLED)
        self.assertEqual(
            self._resources()[(RESOURCE_SNAPSHOT, SNAPSHOT_REF)],
            STATE_RETAINED,
        )


class SnapshotRefusedTest(LateSplitCase, unittest.TestCase):
    """Nothing is created on the strength of a snapshot nobody could prove."""

    def test_a_refusal_creates_no_child_and_parks(self) -> None:
        for described, seed in REFUSALS:
            with self.subTest(refusal=described):
                self.setUp()

                outcome = self._transact(snapshot=seed)

                self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
                self.assertEqual(self.github.created_child_issues, [])
                self.assertIsNone(self._pinned().get(KEY_CHILDREN))
                self.assertEqual(
                    self._pinned().get(KEYS.park_reason), PARK_SNAPSHOT_FAILED,
                )

    def test_a_refusal_leaves_the_obligation(self) -> None:
        # The create may have landed and the verification may be what failed,
        # so a reclamation is left a ref to ask about rather than a gap.
        self._transact(snapshot=unproven_snapshot(SnapshotOutcome.MISMATCH))

        self.assertEqual(
            self._resources()[(RESOURCE_SNAPSHOT, SNAPSHOT_REF)], STATE_FAILED,
        )

    def test_a_refusal_reports_the_typed_failure(self) -> None:
        self._transact(snapshot=refused_snapshot(SnapshotOutcome.UNREADABLE))

        self.assertEqual(
            [record["failure"] for record in
             self._events_named(EVENT_LATE_FAILURE)],
            [FAILURE_SNAPSHOT],
        )

    def test_a_refusal_keeps_the_recorded_verdict(self) -> None:
        # What makes the retry free: the candidate, the measurement, and the
        # identities are all exactly as the adjudication left them.
        self._transact(snapshot=refused_snapshot(SnapshotOutcome.REFUSED))

        pinned = self._pinned()
        self.assertEqual(pinned[KEYS.candidate_sha], CANDIDATE_SHA)
        self.assertEqual(pinned[KEYS.base_sha], self.generation.base_sha)


class SnapshotIdentityTest(LateSplitCase, unittest.TestCase):
    """A ref is built from the identities and refused where they are damaged."""

    def test_a_damaged_identity_creates_nothing(self) -> None:
        # The fields come out of a pinned comment a human can edit, and a ref
        # assembled from a damaged one is one this orchestrator could push and
        # never recognize again.
        damaged = replace(self.generation, cycle_id=0)

        with self.assertLogs(level="ERROR"):
            outcome = self._transact(generation=damaged)

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(self.github.created_child_issues, [])

    def test_an_opaque_ledger_stops_everything(self) -> None:
        # A ledger this binary cannot read is written back verbatim, so an
        # update merged into the typed view would vanish at the next write --
        # taking the ref nobody would then reclaim with it.
        opaque = replace(self.generation, opaque_resources=OPAQUE_LEDGER)

        outcome = self._transact(generation=opaque)

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(self.github.created_child_issues, [])
        self.assertEqual(
            self._pinned().get(KEYS.park_reason), PARK_CHILDREN_FAILED,
        )

    def test_the_owner_is_the_module_the_stage_names(self) -> None:
        # A mock aimed anywhere else would let the real transport run.
        self.assertIn("_snapshot_for_split", _late_snapshot.__dict__)


if __name__ == "__main__":
    unittest.main()
