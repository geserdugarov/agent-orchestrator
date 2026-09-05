# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What one clean auto rebase hands the size gate, and what the gate does with it.

The refresh is the seam: it knows the pre-rebase anchor its force-push is
leased against, the head the replay left, and the base that replay was made
onto, and none of the three survives anything else running. What it does with
them is assemble the evidence a transfer is decided on -- beside the pair the
adjudication already recorded, which is the only pair a verdict may move off.

Nothing here rules on that evidence. What these cases pin is the wiring: the
terms the gate is handed, that an equivalent replay publishes and rotates
without a reading, that a base advance which changed the contribution falls
back to the ordinary cumulative gate, and that evidence this refresh cannot
assemble is no claim at all. What a process that dies inside the same tick
comes back to belongs to the recovery that classifies it, and lives beside
that owner's own cases.
"""
from __future__ import annotations

import unittest

from orchestrator.git.measurement.models import (
    FrozenCommit,
    MeasurementFailure,
)
from orchestrator.workflow.late_split import (
    exemption as _exemption,
    rewrites as _rewrites,
)
from orchestrator.workflow.state import WorkflowLabel
from tests.git.base_sync.exemption_test_support import (
    ACCEPTED_BASE_SHA,
    ACCEPTED_DIGEST,
    ACCEPTED_SHA,
    CHANGED_DIGEST,
    EVENT_MEASUREMENT,
    EVENT_TRANSFER,
    LEASE,
    REPLAYED_BASE_SHA,
    REVISION,
    _CleanRebaseCase,
    adjudicated,
)
from tests.git.base_sync.refresh_scenarios import PUSH_PATCH
from tests.git.base_sync.refresh_test_support import (
    AFTER_SHA,
    BEFORE_SHA,
    ISSUE,
    KEY_PARK_REASON,
    KEY_REVIEW_ROUND,
    LABEL_VALIDATING,
    PARK_PUSH_FAILED,
    PR_NUMBER,
)
from tests.workflow.fixtures import LABEL_DECOMPOSING


class TransferredRebaseTest(_CleanRebaseCase, unittest.TestCase):
    """The replay that contributes what the adjudication already accepted."""

    def setUp(self) -> None:
        super().setUp()
        adjudicated(self)
        self.scenario = self._rebases()

    def test_the_evidence_names_the_whole_rewrite(self) -> None:
        # The pair the verdict came from is the one the record already holds;
        # the pair it goes to is this rebase's own, and the anchor beside it
        # is the head the force-push was leased against rather than a third
        # spelling of the commit that was replaced.
        authorized = _rewrites.read_rewrite_authorization(self._durable())

        self.assertEqual(authorized.rewrite, _rewrites.LateRewrite(
            kind=_rewrites.LateRewriteKind.AUTO_CLEAN_REBASE,
            from_sha=BEFORE_SHA,
            from_base_sha=ACCEPTED_BASE_SHA,
            to_sha=AFTER_SHA,
            to_base_sha=REPLAYED_BASE_SHA,
            pr_number=PR_NUMBER,
            source_stage=WorkflowLabel.IN_REVIEW,
            lease=BEFORE_SHA,
        ))

    def test_the_receipt_carries_the_exemption_over(self) -> None:
        durable = self._durable()

        self.assertTrue(_exemption.is_exempt(durable, AFTER_SHA))
        identity = _exemption.read_semantic_identity(durable)
        self.assertEqual(identity.base_sha, REPLAYED_BASE_SHA)
        self.assertEqual(
            _rewrites.read_rewrite_authorization(durable).phase,
            _rewrites.LateRewritePhase.PUBLISHED,
        )

    def test_no_generation_or_adjudication_is_created(self) -> None:
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])
        self.assertNotIn((ISSUE, LABEL_DECOMPOSING), self.gh.label_history)
        self.assertEqual(len(self._events_of(EVENT_TRANSFER)), 1)

    def test_the_refresh_tail_is_unchanged(self) -> None:
        # The push is still named against the replay and pinned to the anchor,
        # and the reviewer is still sent back to it.
        pushed = self.scenario[PUSH_PATCH].call_args.kwargs
        self.assertEqual(pushed[REVISION], AFTER_SHA)
        self.assertEqual(pushed[LEASE], BEFORE_SHA)
        self.assertIn((ISSUE, LABEL_VALIDATING), self.gh.label_history)
        self.assertEqual(self.gh.pinned_data(ISSUE)[KEY_REVIEW_ROUND], 0)


class MeasuredRebaseTest(_CleanRebaseCase, unittest.TestCase):
    """The replays no transfer is granted for, read by the cumulative gate."""

    def test_a_changed_contribution_is_measured(self) -> None:
        # A base advance that moved what the branch adds to it produces a
        # contribution nobody adjudicated, so the cumulative gate reads it.
        adjudicated(self)
        self.reading.digests[(REPLAYED_BASE_SHA, AFTER_SHA)] = CHANGED_DIGEST

        self._rebases()

        self._assert_measured()

    def test_a_legacy_exemption_claims_nothing(self) -> None:
        # A comment with no semantic record has no accepted pair to name, so
        # the refresh assembles no evidence at all.
        adjudicated(self, identity=False)

        self._rebases()

        self._assert_measured()

    def test_an_unnameable_base_claims_nothing(self) -> None:
        # The base a replay sits over is one end of the contribution it
        # produced, and it is the REMOTE's answer rather than a local ref the
        # agent can repoint. A remote that would not name the branch leaves
        # nothing to fingerprint the replay over, so no transfer is claimed --
        # and the ordinary reading, which freezes the same base, cannot be
        # taken either.
        adjudicated(self)
        self.reading.base = FrozenCommit(
            failure=MeasurementFailure.BASE_UNREADABLE, detail="no token",
        )

        self._rebases()

        self.assertEqual(self._events_of(EVENT_TRANSFER), [])
        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, BEFORE_SHA))
        self.assertFalse(_rewrites.carries_rewrite_authorization(durable))


class RolledBackRebaseTest(_CleanRebaseCase, unittest.TestCase):
    """What a refused push owes when the permit was granted off the anchor."""

    def test_a_refused_push_drops_the_permission(self) -> None:
        # The commit a human ruled on and the head this rebase found are two
        # commits carrying one contribution, which is all a permit is granted
        # on -- so the permission names the accepted commit while the anchor
        # it leases against is the branch's own head. A push the remote
        # refuses resets the branch back onto that anchor, leaving the
        # rewritten commit on no branch, so the permission goes with it.
        adjudicated(self, accepted=ACCEPTED_SHA)
        self.reading.digests[(ACCEPTED_BASE_SHA, ACCEPTED_SHA)] = (
            ACCEPTED_DIGEST
        )

        self._rebases(push_result=False)

        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, ACCEPTED_SHA))
        self.assertFalse(_rewrites.carries_rewrite_authorization(durable))
        pinned = self.gh.pinned_data(ISSUE)
        self.assertEqual(pinned[KEY_PARK_REASON], PARK_PUSH_FAILED)


if __name__ == "__main__":
    unittest.main()
