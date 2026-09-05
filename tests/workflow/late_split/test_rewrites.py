# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What authorized an exemption to move, and every way it authorizes nothing."""
from __future__ import annotations

import unittest
from types import MappingProxyType

from orchestrator.git.measurement.models import FINGERPRINT_FORMAT
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import (
    exemption as _exemption,
    keys as _late_keys,
    rewrites as _rewrites,
    state as _late_state,
)
from orchestrator.workflow.late_split.formats import InvalidLateValue
from orchestrator.workflow.state import WorkflowLabel
from tests.workflow.late_split.generation_test_support import (
    BASE_SHA,
    CANDIDATE_SHA,
    DIGEST_LENGTH,
    PUBLISHED_PR_NUMBER,
    SHA_LENGTH,
    measured_generation,
)

# The stage a squash record names, which is the stage that makes one: the
# approval handoff runs the rewrite before it relabels. Spelled here rather
# than borrowed from the generation fixtures beside it, because a record is
# held to the stage its own KIND is entered from and the two vocabularies only
# happen to overlap.
SQUASH_STAGE = WorkflowLabel.VALIDATING

# The pair one squash turns into another: the commit a human adjudicated, and
# the object the rewrite replaced it with over the same merge base.
REWRITTEN_SHA = "9" * SHA_LENGTH
MERGE_BASE_SHA = "8" * SHA_LENGTH
CONTRIBUTION_DIGEST = "e" * DIGEST_LENGTH
# A digest cut in half: hex, and a hash of nothing anything could be
# compared against, which is why the field is read at its exact length.
_HALF_A_DIGEST = DIGEST_LENGTH // 2
# The head the pull request stood on before the force-push, which for a squash
# is the accepted commit itself.
LEASE_SHA = CANDIDATE_SHA

_KIND = _rewrites.LATE_REWRITE_KIND
_PHASE = _rewrites.LATE_REWRITE_PHASE
_FROM = _rewrites.LATE_REWRITE_FROM_SHA
_FROM_BASE = _rewrites.LATE_REWRITE_FROM_BASE_SHA
_TO = _rewrites.LATE_REWRITE_TO_SHA
_TO_BASE = _rewrites.LATE_REWRITE_TO_BASE_SHA
_FINGERPRINT = _rewrites.LATE_REWRITE_FINGERPRINT
_FORMAT = _rewrites.LATE_REWRITE_FINGERPRINT_FORMAT
_PR_NUMBER = _rewrites.LATE_REWRITE_PR_NUMBER
_STAGE = _rewrites.LATE_REWRITE_SOURCE_STAGE
_LEASE = _rewrites.LATE_REWRITE_LEASE
# Deliberately outside the group below: the note is about the record a settled
# transfer owes the sinks rather than about the permission it was granted on.
_PROOF = _rewrites.LATE_REWRITE_PROOF

_AUTHORIZATION_KEYS = (
    _KIND, _PHASE, _FROM, _FROM_BASE, _TO, _TO_BASE,
    _FINGERPRINT, _FORMAT, _PR_NUMBER, _STAGE, _LEASE,
)

# Every pinned comment that CLAIMS a transfer and cannot show one. None is the
# field being absent -- a comment written before this group existed, or one a
# crash left half written -- and anything else is a value nothing here would
# have written: an abbreviated end, a truncated digest, a kind and a phase
# from some other build, a version this one does not compute, a pull request
# that is not an identity, a stage no publication is entered from, and a
# rewritten commit that is not the one the exemption names.
_UNUSABLE_RECORDS = MappingProxyType({
    "no kind": {_KIND: None},
    "no phase": {_PHASE: None},
    "no accepted commit": {_FROM: None},
    "no accepted base": {_FROM_BASE: None},
    "no rewritten commit": {_TO: None},
    "no rewritten base": {_TO_BASE: None},
    "no fingerprint": {_FINGERPRINT: None},
    "no format": {_FORMAT: None},
    "no pull request": {_PR_NUMBER: None},
    "no source stage": {_STAGE: None},
    "no lease": {_LEASE: None},
    "a kind this build does not authorize": {_KIND: "amend"},
    "a kind the recorded stage does not make": {
        _KIND: str(_rewrites.LateRewriteKind.CONFLICT_REBASE),
    },
    "a stage that makes the other kind": {
        _STAGE: str(WorkflowLabel.RESOLVING_CONFLICT),
    },
    "a phase this build does not write": {_PHASE: "reverted"},
    "an abbreviated accepted commit": {_FROM: CANDIDATE_SHA[:7]},
    "a truncated fingerprint": {_FINGERPRINT: CONTRIBUTION_DIGEST[:_HALF_A_DIGEST]},
    "a format nothing here computes": {_FORMAT: FINGERPRINT_FORMAT + 1},
    "a pull request that is not an identity": {_PR_NUMBER: 0},
    "a stage no publication is entered from": {
        _STAGE: str(WorkflowLabel.READY),
    },
    "an accepted commit nothing exempts": {_FROM: BASE_SHA},
})

# What a caller can hand the write that is not the shape the field takes.
_REFUSED_WRITES = MappingProxyType({
    "a kind this build does not authorize": {"kind": "amend"},
    "a kind this stage does not make": {
        "kind": _rewrites.LateRewriteKind.CONFLICT_REBASE,
    },
    "a stage that makes the other kind": {
        "source_stage": WorkflowLabel.RESOLVING_CONFLICT,
    },
    "a pull request that is not an identity": {"pr_number": 0},
    "a stage no publication is entered from": {
        "source_stage": WorkflowLabel.READY,
    },
    "an abbreviated accepted commit": {"from_sha": CANDIDATE_SHA[:7]},
    "a base that is prose": {"to_base_sha": "the merge base"},
    "a lease that is not a commit": {"lease": 7},
})


# Which reading a settlement was proved by, which the record now
# carries until the report it owes has been made.
_SETTLING_PROOF = _rewrites.LateRewriteProof.PUSHED


def granted_rewrite(**overrides) -> _rewrites.LateRewrite:
    """The squash a permit is granted over, with any term replaced."""
    return _rewrites.LateRewrite(**{
        "kind": _rewrites.LateRewriteKind.SQUASH,
        "from_sha": CANDIDATE_SHA,
        "from_base_sha": MERGE_BASE_SHA,
        "to_sha": REWRITTEN_SHA,
        "to_base_sha": MERGE_BASE_SHA,
        "pr_number": PUBLISHED_PR_NUMBER,
        "source_stage": SQUASH_STAGE,
        "lease": LEASE_SHA,
        **overrides,
    })


def authorized_state() -> PinnedState:
    """One granted permission's whole record, as the write leaves it.

    The exemption is still the commit a human ruled on, because a grant moves
    nothing: what it records is what a later write may move, and until that
    write lands the accepted end is what binds the record to the exemption.
    """
    state = PinnedState(data={})
    _exemption.record_exemption(state, CANDIDATE_SHA)
    _rewrites.record_rewrite_authorization(
        state, granted_rewrite(), CONTRIBUTION_DIGEST,
    )
    return state


def damaged_state(damage: dict) -> PinnedState:
    """That record with one field absent, or carrying what nobody here wrote."""
    state = authorized_state()
    for key, written in damage.items():
        if written is None:
            state.data.pop(key, None)
        else:
            state.data[key] = written
    return state


class RecordedAuthorizationTest(unittest.TestCase):
    """What one granted transfer round trips as, and what it refuses to."""

    def test_the_granted_transfer_round_trips(self) -> None:
        authorization = _rewrites.read_rewrite_authorization(
            authorized_state(),
        )

        self.assertEqual(authorization.rewrite, granted_rewrite())
        self.assertEqual(authorization.fingerprint, CONTRIBUTION_DIGEST)
        self.assertEqual(authorization.fingerprint_format, FINGERPRINT_FORMAT)
        self.assertEqual(
            authorization.phase, _rewrites.LateRewritePhase.AUTHORIZED,
        )

    def test_no_group_authorizes_nothing(self) -> None:
        state = PinnedState(data={})

        self.assertFalse(_rewrites.carries_rewrite_authorization(state))
        self.assertIsNone(_rewrites.read_rewrite_authorization(state))

    def test_another_commits_authorization_refuses(self) -> None:
        # The accepted end has to BE the commit the exemption names, because
        # the whole of what the record licenses is moving that one verdict.
        # Written beside some other commit it would license a move this issue
        # never earned.
        state = PinnedState(data={})
        _exemption.record_exemption(state, BASE_SHA)

        with self.assertRaises(InvalidLateValue):
            _rewrites.record_rewrite_authorization(
                state, granted_rewrite(), CONTRIBUTION_DIGEST,
            )

        self.assertFalse(_rewrites.carries_rewrite_authorization(state))

    def test_unrecordable_terms_are_refused(self) -> None:
        for described, overrides in _REFUSED_WRITES.items():
            with self.subTest(written=described):
                state = PinnedState(data={})
                _exemption.record_exemption(state, CANDIDATE_SHA)

                with self.assertRaises(InvalidLateValue):
                    _rewrites.record_rewrite_authorization(
                        state, granted_rewrite(**overrides),
                        CONTRIBUTION_DIGEST,
                    )

                self.assertFalse(
                    _rewrites.carries_rewrite_authorization(state),
                )

    def test_a_truncated_digest_is_refused(self) -> None:
        state = PinnedState(data={})
        _exemption.record_exemption(state, CANDIDATE_SHA)

        with self.assertRaises(InvalidLateValue):
            _rewrites.record_rewrite_authorization(
                state, granted_rewrite(), CONTRIBUTION_DIGEST[:_HALF_A_DIGEST],
            )

    def test_it_outlives_the_generation(self) -> None:
        # It describes the exemption, which is the one record written so the
        # generation CAN be cleared -- so the clear may take neither.
        state = authorized_state()
        _late_state.write_late_generation(state, measured_generation())

        _late_state.clear_late_generation(state)

        self.assertIsNotNone(_rewrites.read_rewrite_authorization(state))
        for key in _AUTHORIZATION_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, _late_keys.LATE_STATE_KEYS)

    def test_clearing_takes_the_whole_group(self) -> None:
        state = authorized_state()

        _rewrites.clear_rewrite_authorization(state)

        self.assertFalse(_rewrites.carries_rewrite_authorization(state))
        self.assertEqual(
            _exemption.read_exemption(state), CANDIDATE_SHA,
        )


class SpentAuthorizationTest(unittest.TestCase):
    """The write that carries the exemption over, and what it leaves behind.

    The move itself, what it refuses to make, and the note it keeps: which
    reading proved the push landed is the one fact nothing later could
    re-derive, so it stands on the comment until the record it feeds is out.
    """

    def test_the_whole_move_lands_in_one_write(self) -> None:
        # The exemption, what the commit it names contributes, and the phase
        # that says the transfer is over are one record: a reader is entitled
        # to find them agreeing, and any two of them apart is a comment no
        # reader here can tell from a hand edit.
        state = authorized_state()

        spent = _rewrites.record_rewrite_publication(state, _SETTLING_PROOF)

        self.assertEqual(spent, granted_rewrite())
        self.assertEqual(_exemption.read_exemption(state), REWRITTEN_SHA)
        identity = _exemption.read_semantic_identity(state)
        self.assertEqual(identity.base_sha, MERGE_BASE_SHA)
        self.assertEqual(identity.candidate_sha, REWRITTEN_SHA)
        self.assertEqual(identity.fingerprint, CONTRIBUTION_DIGEST)
        authorization = _rewrites.read_rewrite_authorization(state)
        self.assertEqual(
            authorization.phase, _rewrites.LateRewritePhase.PUBLISHED,
        )
        self.assertEqual(authorization.rewrite, granted_rewrite())

    def test_a_spent_permission_is_not_outstanding(self) -> None:
        state = authorized_state()

        _rewrites.record_rewrite_publication(state, _SETTLING_PROOF)

        self.assertFalse(_rewrites.outstanding_permission(state))

    def test_a_settlement_owes_its_own_reading(self) -> None:
        # The record goes to the sinks behind the write that settles the
        # transfer, and which reading proved the push landed is the one fact
        # nothing later could re-derive -- the receipt looks identical either
        # way. So it is kept until the report is out.
        state = _published_state()

        self.assertEqual(
            _rewrites.unreported_transfer(state), _SETTLING_PROOF,
        )
        self.assertFalse(_rewrites.stranded_transfer_proof(state))

        _rewrites.forget_transfer_proof(state)

        self.assertIsNone(_rewrites.unreported_transfer(state))
        self.assertFalse(_rewrites.stranded_transfer_proof(state))

    def test_an_unreportable_proof_is_damage(self) -> None:
        # Presence is what tells these apart from a comment that owes
        # nothing, and each of them answers None for the report: read as
        # "nothing owed", the recovery finishes a route over a settled
        # transfer no sink ever heard about.
        stranded = {
            "a reading this build does not know": self._damaged_proof(
                {_PROOF: "not-a-reading"},
            ),
            "a phase the settlement never reached": self._damaged_proof(
                {_PHASE: str(_rewrites.LateRewritePhase.AUTHORIZED)},
            ),
            "a permission short of a member": self._damaged_proof(
                {_LEASE: None},
            ),
        }
        for described, state in stranded.items():
            with self.subTest(standing=described):
                self.assertIsNone(_rewrites.unreported_transfer(state))
                self.assertTrue(_rewrites.stranded_transfer_proof(state))

    def test_a_grant_drops_the_proof_it_replaces(self) -> None:
        # A grant replaces the whole group, so the proof beside it describes
        # the transfer being replaced -- and the phase going back to
        # `authorized` is what would leave it unreadable. Only a report whose
        # own drop-write GitHub refused gets one this far, and that record
        # has already been made.
        state = _published_state()
        _exemption.record_exemption(state, REWRITTEN_SHA)

        _rewrites.record_rewrite_authorization(
            state,
            granted_rewrite(from_sha=REWRITTEN_SHA, to_sha=CANDIDATE_SHA),
            CONTRIBUTION_DIGEST,
        )

        self.assertFalse(_rewrites.stranded_transfer_proof(state))
        self.assertIsNone(_rewrites.unreported_transfer(state))

    def test_it_refuses_a_record_it_cannot_vouch_for(self) -> None:
        # Every way a permission fails to be one this build granted: nothing
        # standing at all, a group damaged past reading, and one already
        # spent. Each would move a human's verdict on evidence nobody checked.
        refused = {
            "no permission at all": PinnedState(data={}),
            "a damaged permission": damaged_state({_LEASE: None}),
            "a permission already spent": _published_state(),
        }
        for described, state in refused.items():
            with self.subTest(standing=described):
                before = dict(state.data)

                with self.assertRaises(InvalidLateValue):
                    _rewrites.record_rewrite_publication(state, _SETTLING_PROOF)

                self.assertEqual(state.data, before)

    def _damaged_proof(self, damage: dict) -> PinnedState:
        """A settled transfer whose proof stands over one edited field."""
        state = _published_state()
        for key, written in damage.items():
            if written is None:
                state.data.pop(key, None)
            else:
                state.data[key] = written
        return state


def _published_state() -> PinnedState:
    """The comment one settled transfer leaves, through the write that makes it."""
    state = authorized_state()
    _rewrites.record_rewrite_publication(state, _SETTLING_PROOF)
    return state


class DamagedAuthorizationTest(unittest.TestCase):
    """A claim this domain cannot vouch for is a claim, and it is not read.

    The two halves of one contract: a record short of anything reads back as
    no authorization, so nothing reverses a human's verdict on it -- while the
    comment still SAYS a transfer happened, which is what keeps a damaged one
    from looking like a comment that never carried a group at all.
    """

    def test_a_damaged_record_authorizes_nothing(self) -> None:
        for described, damage in _UNUSABLE_RECORDS.items():
            with self.subTest(record=described):
                state = damaged_state(damage)

                self.assertIsNone(
                    _rewrites.read_rewrite_authorization(state),
                )

    def test_a_null_member_is_still_a_claim(self) -> None:
        # A pinned comment is JSON, so a field can be PRESENT and null -- a
        # hand edit, or an older binary writing a value this one reads as
        # nothing. Asked for a value rather than for the key, the minimal
        # damaged group would answer "no group at all" and be overwritten.
        state = PinnedState(data={_TO: None})

        self.assertTrue(_rewrites.carries_rewrite_authorization(state))
        self.assertTrue(_rewrites.claims_the_exemption(state))
        self.assertIsNone(_rewrites.read_rewrite_authorization(state))

    def test_a_damaged_record_is_still_a_claim(self) -> None:
        for described, damage in _UNUSABLE_RECORDS.items():
            with self.subTest(record=described):
                state = damaged_state(damage)

                self.assertTrue(
                    _rewrites.carries_rewrite_authorization(state),
                )

    def test_a_moved_exemption_stops_the_record(self) -> None:
        # The identity the exemption write drops takes the authorization's
        # meaning with it: a record describing a move onto a commit nothing
        # exempts any more is one no rollback may act on.
        state = authorized_state()

        _exemption.record_exemption(state, BASE_SHA)

        self.assertIsNone(_rewrites.read_rewrite_authorization(state))
