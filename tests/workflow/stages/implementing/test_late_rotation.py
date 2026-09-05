# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What the receipt of a landed push does with the permission that licensed it.

The far end of the transfer, driven through the shared gated-publication push
tail rather than through the owner alone, because the two facts the settlement
turns on are made by that tail: the commit the push named, and the head the
entry froze the pull request at. Both roads a permit accounts for are here --
a remote still standing where the grant left it, and one a tick that pushed
and died before its receipt already moved -- and each is asserted on the
durable comment, the push the tail issued, and the one record it left.
"""
from __future__ import annotations

import unittest
from types import MappingProxyType
from unittest.mock import patch

from orchestrator import config
from orchestrator.workflow.late_split import (
    exemption as _exemption,
    rewrites as _rewrites,
)
from orchestrator.workflow.stages.implementing import (
    late_claims as _claims,
    late_push as _push,
    late_reconcile as _reconcile,
    late_records as _records,
    late_rotation as _rotation,
    state as _state,
)
from orchestrator.workflow.state import WorkflowLabel
from tests.workflow.stages.implementing import late_transfer_test_support as _support

ACCEPTED_SHA = _support.ACCEPTED_SHA
MERGE_BASE_SHA = _support.MERGE_BASE_SHA
REWRITTEN_SHA = _support.REWRITTEN_SHA
LEASED_SHA = _support.LEASED_SHA
ACCEPTED_DIGEST = _support.ACCEPTED_DIGEST
PR_NUMBER = _support.PR_NUMBER
ISSUE_NUMBER = _support.ISSUE_NUMBER

# The two keywords a gated push names its commit and pins its ref by.
REVISION = "revision"
LEASE = "force_with_lease"

# The receipt a landed gated push leaves, and the head it replaced.
KEY_RECEIPT_SHA = "implementing_published_sha"
KEY_RECEIPT_LEASE = "implementing_published_lease"

EVENT_TRANSFER = "late_transfer"
EVENT_VERDICT = "late_verdict"

PINNED_WRITE = "write_pinned_state"

# The stage the transfer was entered from, as both sinks spell it.
STAGE_TAG = "validating"

# The state a relabel moved the issue to while the rewrite was being made,
# which the permit's own re-read of the issue is the only reading that sees.
RELABELLED = "workflow:fixing"

# The ceiling the fallback reading is taken against, high enough that the
# rewritten commit publishes on its count once the permit has refused.
MAX_ADDED_LINES = "MAX_ADDED_LINES"
CEILING = 100

# The note a settled transfer keeps until its record is out, and the seam that
# record goes out through -- which is where a process lost behind the receipt
# is stopped.
KEY_TRANSFER_PROOF = "late_rewrite_proof"
REPORTS_THE_TRANSFER = "_reports_the_transfer"

# The park a record nothing can read earns, and the flag beside it.
KEY_PARK_REASON = "park_reason"
PARK_DAMAGED = "late_measurement_failed"

# Every way the note a settled transfer keeps can stand and say nothing: a
# reading this build does not know, a permission short of a member, and a
# phase the settlement never reached. Each is what the reader that makes the
# record answers None to, and none of them is an absence.
UNKNOWN_READING = "not-a-reading"

_STRANDED_PROOFS = MappingProxyType({
    "a reading this build does not know": {
        KEY_TRANSFER_PROOF: UNKNOWN_READING,
    },
    "a permission short of a member": {_rewrites.LATE_REWRITE_LEASE: None},
    "a phase the settlement never reached": {
        _rewrites.LATE_REWRITE_PHASE: str(
            _rewrites.LateRewritePhase.AUTHORIZED,
        ),
    },
})


class _Interrupted(RuntimeError):
    """The process dying at the seam a case is about."""


class _DiesBeforeTheReport:
    """A tick whose receipt lands and whose record never goes out.

    The one window behind the settlement's own write, staged where it really
    is: the exemption, the identity, the phase, and the receipt are all
    durable, and the report the write behind them owed never happened.
    """

    def __call__(self, gate, rotation) -> None:
        """End the tick at the step that would have made the record."""
        raise _Interrupted("died before the transfer was reported")


class _RefusesTheReceipt:
    """A comment GitHub takes for the grant and refuses for the receipt.

    The narrow outage the settlement has to survive: the branch is on the
    remote and the write that would say so is lost, so nothing may be believed
    durable -- least of all a verdict, which would then name a commit no
    receipt accounts for.
    """

    def __init__(self, github) -> None:
        self.writes = 0
        self._writes = github.write_pinned_state

    def __call__(self, issue, state):
        self.writes += 1
        if self.writes > 1:
            raise RuntimeError("pinned comment rejected")
        return self._writes(issue, state)


class _SettlementCase(unittest.TestCase):
    """One gated push made over an issue whose exemption is about to move."""

    def setUp(self) -> None:
        adjudicated = _support.adjudicated()
        self.github = adjudicated.github
        self.issue = adjudicated.issue
        self.state = adjudicated.state
        self.readings = _support.readings(self)
        self.pushed = None
        self.published = None

    def _publishes(self, *, standing: str, granted: bool, **overrides) -> None:
        """Run the push tail over a remote standing on this head.

        `granted` seeds the comment a permit's own write already left, which
        is what a recovery answers from: the tick that granted it pushed and
        did not get its receipt down. A fresh transfer hands the evidence in
        instead, exactly as the squash that made the rewrite does.
        """
        _support.open_pull_request(self.github, standing)
        if granted:
            _support.granted(self.state)
            self.github.write_pinned_state(self.issue, self.state)
        self.pushed = self.enterContext(
            _support.seam_patch(_support.PUSH_BRANCH),
        )
        self.pushed.return_value = True
        self.published = _push._publishes(
            _support.gate(
                self.github, self.issue, self.state,
                candidate="", entry=None, rewrite=None,
            ),
            _support.BRANCH,
            _records._Entered(**{
                "stage": _support.SOURCE_STAGE,
                "head": LEASED_SHA,
                "candidate": REWRITTEN_SHA,
                "reconciling": True,
                "answering": granted,
                "rewrite": None if granted else _support.rewrite(),
                **overrides,
            }),
        )

    def _records_of(self, family: str) -> list[dict]:
        return [
            record for record in self.github.recorded_events
            if record.get("event") == family
        ]

    def _reported(self) -> dict:
        """The one transfer record this tick left on the audit stream."""
        reported = self._records_of(EVENT_TRANSFER)
        self.assertEqual(len(reported), 1)
        return reported[0]

    def _durable(self):
        """The pinned comment as a process starting now would read it."""
        return self.github.read_pinned_state(self.issue)

    def _assert_carried(self) -> None:
        """The verdict is on the rewritten commit, with what it contributes."""
        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, REWRITTEN_SHA))
        identity = _exemption.read_semantic_identity(durable)
        self.assertEqual(identity.base_sha, MERGE_BASE_SHA)
        self.assertEqual(identity.candidate_sha, REWRITTEN_SHA)
        self.assertEqual(identity.fingerprint, ACCEPTED_DIGEST)
        self.assertEqual(
            _rewrites.read_rewrite_authorization(durable).phase,
            _rewrites.LateRewritePhase.PUBLISHED,
        )

    def _assert_left_put(self) -> None:
        """The verdict is exactly where the adjudication put it."""
        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, ACCEPTED_SHA))
        self.assertEqual(
            _exemption.read_semantic_identity(durable).candidate_sha,
            ACCEPTED_SHA,
        )


class LandedTransferTest(_SettlementCase):
    """The push that moves the pull request onto the rewritten commit."""

    def setUp(self) -> None:
        super().setUp()
        self._publishes(standing=LEASED_SHA, granted=False)

    def test_the_exemption_moves_with_the_receipt(self) -> None:
        self.assertTrue(self.published.landed)
        self._assert_carried()
        pinned = self._durable().data
        self.assertEqual(pinned[KEY_RECEIPT_SHA], REWRITTEN_SHA)
        self.assertEqual(pinned[KEY_RECEIPT_LEASE], LEASED_SHA)

    def test_the_push_is_named_and_leased(self) -> None:
        # The grant licenses a push and nothing about how it is made: the
        # commit that was proved is what goes out, pinned to the head the
        # permit was granted against, so a pull request somebody moved in
        # between rejects it.
        self.pushed.assert_called_once()
        pushed = self.pushed.call_args.kwargs
        self.assertEqual(pushed[REVISION], REWRITTEN_SHA)
        self.assertEqual(pushed[LEASE], LEASED_SHA)

    def test_the_debt_the_grant_recorded_is_paid(self) -> None:
        pinned = self._durable().data
        self.assertIsNone(pinned.get(_state._APPROVED_SHA))
        self.assertIsNone(pinned.get(_state._APPROVED_LEASE))

    def test_the_record_names_both_pairs(self) -> None:
        # Both ends of both contributions, which is the whole of what says the
        # change carried over is the change a human ruled on.
        recorded = self._reported()

        self.assertEqual(recorded["transferred_from_sha"], ACCEPTED_SHA)
        self.assertEqual(recorded["transferred_from_base_sha"], MERGE_BASE_SHA)
        self.assertEqual(recorded["source_sha"], REWRITTEN_SHA)
        self.assertEqual(recorded["base_sha"], MERGE_BASE_SHA)

    def test_the_record_names_the_publication(self) -> None:
        recorded = self._reported()

        self.assertEqual(recorded["issue"], ISSUE_NUMBER)
        self.assertEqual(recorded["stage"], STAGE_TAG)
        self.assertEqual(recorded["published_pr_number"], PR_NUMBER)
        self.assertEqual(recorded["rewrite_kind"], "squash")
        self.assertEqual(recorded["transfer_proof"], "pushed")

    def test_no_second_verdict_is_reported(self) -> None:
        # A transfer carries a decision a human already made onto the object
        # that replaced the one they made it about. A `single` on the stream
        # here would read as a second adjudication of the same work.
        self.assertEqual(self._records_of(EVENT_VERDICT), [])


class AlreadyLandedTransferTest(_SettlementCase):
    """The retry that finds the pull request already on the rewritten commit.

    A tick that pushed and died before its receipt leaves the permission
    outstanding and the remote where its own push put it. The permit is
    re-asked in full over the record the grant left, the push is the leased
    no-op that proves the pull request is still standing there, and the
    receipt behind it settles the transfer the first tick could not.
    """

    def setUp(self) -> None:
        super().setUp()
        self._publishes(standing=REWRITTEN_SHA, granted=True)

    def test_the_lost_receipt_settles_the_transfer(self) -> None:
        self.assertTrue(self.published.landed)
        self._assert_carried()
        self.assertEqual(self._durable().data[KEY_RECEIPT_SHA], REWRITTEN_SHA)

    def test_the_no_op_is_leased_against_the_commit(self) -> None:
        # Never unleased, and never skipped: what the request buys is proof
        # taken at the remote that the publication is still the one the record
        # is about, which no local note could supply.
        self.pushed.assert_called_once()
        pushed = self.pushed.call_args.kwargs
        self.assertEqual(pushed[REVISION], REWRITTEN_SHA)
        self.assertEqual(pushed[LEASE], REWRITTEN_SHA)

    def test_the_record_says_which_reading_proved_it(self) -> None:
        recorded = self._reported()

        self.assertEqual(recorded["transfer_proof"], "already_published")
        self.assertEqual(recorded["source_sha"], REWRITTEN_SHA)


class UnreportedTransferTest(_SettlementCase):
    """A settlement whose own record never reached the sinks.

    The receipt and the move are one durable write and the record of them goes
    out BEHIND it, so a process lost in between leaves a verdict that has
    moved and nothing on either sink saying so -- over the one fact no later
    reading could re-derive, which of the two publications proved the push. It
    is a window every rewrite this workflow settles has, since all of them go
    through the same push tail, and only the base refresh has a recovery route
    that would come back for it. What the other two reach instead is the
    reconciliation ahead of every handler, and these say it makes the record
    there.
    """

    def test_a_squash_settlement_is_reported_later(self) -> None:
        # The collapse a reviewer's approval earns. It resumes into the
        # documentation pass, which has nothing to say about a transfer.
        self._assert_reconciled(
            _rewrites.LateRewriteKind.SQUASH, _support.SOURCE_STAGE,
        )

    def test_a_conflict_replay_is_reported_later(self) -> None:
        # The replay `workflow:resolving_conflict` publishes, which bounces
        # the issue back to the reviewer with the same window behind it.
        self._assert_reconciled(
            _rewrites.LateRewriteKind.CONFLICT_REBASE,
            WorkflowLabel.RESOLVING_CONFLICT,
        )

    def test_an_unreportable_proof_parks_the_tick(self) -> None:
        # The same window with the note something took apart: a reading this
        # build does not know, a permission short of a member, and a phase
        # the settlement never reached. Each answers "nothing to report" to
        # the reader that makes the record -- so read no further, the
        # reconciliation walks past all three, the account is never made, the
        # corrupt note stands for the life of the issue, and the stage runs
        # behind a verdict nothing here can account for.
        settled = self._settles_without_reporting(
            _rewrites.LateRewriteKind.SQUASH, _support.SOURCE_STAGE,
        )
        for described, damage in _STRANDED_PROOFS.items():
            with self.subTest(standing=described):
                self._restores(settled, damage)

                self.assertTrue(self._reconciles(_support.SOURCE_STAGE))

                self.assertEqual(
                    _claims._unreadable_record(
                        _support.SOURCE_STAGE, self._durable(),
                    ),
                    _claims._DAMAGED_TRANSFER,
                )
                durable = self._durable()
                self.assertEqual(durable.get(KEY_PARK_REASON), PARK_DAMAGED)
                # Nothing is discarded for it: the note an operator has to
                # repair is exactly what the crash left.
                self.assertIn(KEY_TRANSFER_PROOF, durable.data)
                self.assertEqual(self._records_of(EVENT_TRANSFER), [])

    def test_a_stranded_proof_parks_the_adjudication(self) -> None:
        # The adjudication is asked two of the claims rather than four,
        # because it is mid-way through the reading and the approval and has
        # failed to produce neither. The note is not one of those: nothing
        # writes one it cannot read back, and the statement that settles a
        # transfer puts the note and the phase down together -- so there is
        # no settlement in flight for a refusal to hold up, only a comment
        # something took apart, which the mode below would decide over while
        # the account stayed unreported for the life of the issue.
        settled = self._settles_without_reporting(
            _rewrites.LateRewriteKind.SQUASH, _support.SOURCE_STAGE,
        )
        self._restores(settled, {KEY_TRANSFER_PROOF: UNKNOWN_READING})

        self.assertTrue(self._reconciles(WorkflowLabel.DECOMPOSING))

        durable = self._durable()
        self.assertEqual(
            _claims._unreadable_record(WorkflowLabel.DECOMPOSING, durable),
            _claims._DAMAGED_TRANSFER,
        )
        self.assertEqual(durable.get(KEY_PARK_REASON), PARK_DAMAGED)
        self.assertIn(KEY_TRANSFER_PROOF, durable.data)
        self.assertEqual(self._records_of(EVENT_TRANSFER), [])

    def _restores(self, settled: dict, damage: dict) -> None:
        """Put the receipted comment back, with one field of the note edited."""
        state = self.github.read_pinned_state(self.issue)
        state.data.clear()
        state.data.update(settled)
        for key, written in damage.items():
            if written is None:
                state.data.pop(key, None)
            else:
                state.data[key] = written
        self.github.write_pinned_state(self.issue, state)

    def _assert_reconciled(self, kind, stage) -> None:
        """The record the crashed settlement owed is made by the next tick."""
        self._settles_without_reporting(kind, stage)

        self._reconciles(stage)

        self.assertEqual(self._reported()["rewrite_kind"], str(kind))
        self.assertIsNone(self._durable().get(KEY_TRANSFER_PROOF))

    def _settles_without_reporting(self, kind, stage) -> dict:
        """Land the receipt and lose the record behind it, and say so.

        Answers the comment that crash leaves, so a case about a note
        something took apart edits the real one rather than a comment nothing
        wrote.
        """
        self._entered_on(stage)
        with patch.object(
            _rotation, REPORTS_THE_TRANSFER, _DiesBeforeTheReport(),
        ), self.assertRaises(_Interrupted):
            self._publishes(
                standing=LEASED_SHA,
                granted=False,
                stage=stage,
                rewrite=_support.rewrite(kind=kind, source_stage=stage),
            )

        self._assert_owes_a_record()
        return dict(self._durable().data)

    def _entered_on(self, stage) -> None:
        """Re-seed this case's world on the stage its rewrite is made from."""
        if stage == _support.SOURCE_STAGE:
            return
        adjudicated = _support.adjudicated(labels=(str(stage),))
        self.github = adjudicated.github
        self.issue = adjudicated.issue
        self.state = adjudicated.state

    def _assert_owes_a_record(self) -> None:
        """The move is durable, and nothing anywhere says it happened."""
        self._assert_carried()
        durable = self._durable()
        self.assertEqual(durable.data[KEY_RECEIPT_SHA], REWRITTEN_SHA)
        self.assertEqual(
            durable.get(KEY_TRANSFER_PROOF),
            str(_rewrites.LateRewriteProof.PUSHED),
        )
        self.assertEqual(self._records_of(EVENT_TRANSFER), [])

    def _reconciles(self, stage) -> bool:
        """Run the reconciliation every handler is dispatched behind."""
        return _reconcile._reconciles_published_work(
            self.github, _support.SPEC, self.issue, stage, self._durable(),
        )


class RefusedSettlementTest(_SettlementCase):
    """A receipt GitHub refuses leaves the verdict where it was.

    The window the settlement exists to close, read from the one side that can
    still be wrong: the branch is on the remote and the write that would say
    so did not land. Nothing may be believed durable there -- least of all a
    verdict, which would then name a commit no receipt accounts for.
    """

    def test_a_refused_receipt_moves_nothing(self) -> None:
        refusing = _RefusesTheReceipt(self.github)

        with patch.object(
            self.github, PINNED_WRITE, refusing,
        ), self.assertRaises(RuntimeError):
            self._publishes(standing=LEASED_SHA, granted=False)

        self._assert_left_put()
        self.assertEqual(
            _rewrites.read_rewrite_authorization(self._durable()).phase,
            _rewrites.LateRewritePhase.AUTHORIZED,
        )
        self.assertNotIn(KEY_RECEIPT_SHA, self._durable().data)
        self.assertEqual(self._records_of(EVENT_TRANSFER), [])


class SupersededPermissionTest(_SettlementCase):
    """A permission the commit this push published has gone past."""

    def test_a_rollback_republication_drops_it(self) -> None:
        # The branch went back onto the commit a human ruled on and that is
        # what reached the remote, so the head the permit was granted against
        # is gone and no later tick can be granted it. What is left is a claim
        # about a push that cannot happen, and the verdict never moved.
        _support.granted(self.state)
        self.github.write_pinned_state(self.issue, self.state)
        self.readings.stands_on(ACCEPTED_SHA)

        self._publishes(
            standing=LEASED_SHA, granted=False,
            candidate="", rewrite=None, answering=True,
        )

        self._assert_left_put()
        self.assertFalse(
            _rewrites.carries_rewrite_authorization(self._durable()),
        )
        self.assertEqual(self._records_of(EVENT_TRANSFER), [])


class RefusedPermitTest(unittest.TestCase):
    """A permit that refuses settles nothing, whatever the reading then allows.

    The road the record alone cannot tell from a settled transfer. The
    permission is on the comment, outstanding, and names the very commit that
    reaches the remote -- and the permit `late_transfer` re-asks this tick
    refuses it, because the issue was relabelled while the rewrite was being
    made. That refusal is not a hold: the rewritten commit falls through to
    the ordinary cumulative gate, comes back under the ceiling, and is pushed
    on its count. What it may not do is carry a human's verdict with it.
    """

    def setUp(self) -> None:
        adjudicated = _support.adjudicated(labels=(RELABELLED,))
        self.github = adjudicated.github
        self.issue = adjudicated.issue
        self.state = adjudicated.state
        _support.readings(self)
        _support.measures(self)
        _support.open_pull_request(self.github, LEASED_SHA)
        _support.granted(self.state)
        self.github.write_pinned_state(self.issue, self.state)
        self.pushed = self.enterContext(
            _support.seam_patch(_support.PUSH_BRANCH),
        )
        self.pushed.return_value = True
        with patch.object(config, MAX_ADDED_LINES, CEILING):
            self.published = _push._publishes(
                _support.gate(
                    self.github, self.issue, self.state,
                    candidate="", entry=None, rewrite=None,
                ),
                _support.BRANCH,
                _records._Entered(
                    stage=_support.SOURCE_STAGE,
                    head=LEASED_SHA,
                    candidate=REWRITTEN_SHA,
                    reconciling=True,
                    answering=True,
                ),
            )

    def test_the_fallback_reading_published_it(self) -> None:
        # The premise: the refusal costs the transfer and not the push, so the
        # settlement really does run over a landed publication of the commit
        # the permission names.
        self.assertTrue(self.published.landed)
        self.pushed.assert_called_once()
        self.assertEqual(
            self.pushed.call_args.kwargs[REVISION], REWRITTEN_SHA,
        )
        self.assertEqual(
            self._durable().data[KEY_RECEIPT_SHA], REWRITTEN_SHA,
        )

    def test_the_verdict_does_not_move(self) -> None:
        durable = self._durable()

        self.assertTrue(_exemption.is_exempt(durable, ACCEPTED_SHA))
        identity = _exemption.read_semantic_identity(durable)
        self.assertEqual(identity.candidate_sha, ACCEPTED_SHA)
        self.assertEqual(identity.base_sha, MERGE_BASE_SHA)

    def test_the_permission_is_left_outstanding(self) -> None:
        # Not spent, because no permit vouched for it; not dropped either,
        # because the remote is now on a head the permit accounts for and a
        # later tick whose refusal has cleared can still settle it.
        authorization = _rewrites.read_rewrite_authorization(self._durable())

        self.assertEqual(
            authorization.phase, _rewrites.LateRewritePhase.AUTHORIZED,
        )
        self.assertEqual(authorization.rewrite.to_sha, REWRITTEN_SHA)

    def test_nothing_is_reported_as_a_transfer(self) -> None:
        self.assertEqual(
            [
                record for record in self.github.recorded_events
                if record.get("event") == EVENT_TRANSFER
            ],
            [],
        )

    def _durable(self):
        """The pinned comment as a process starting now would read it."""
        return self.github.read_pinned_state(self.issue)
