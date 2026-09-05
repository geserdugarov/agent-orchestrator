# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Where an interrupted rebase carrying a verdict stopped, and what finishes it.

One auto rebase of an adjudicated commit is six durable moments in a row --
the anchor, the rewrite, the permission, the push, the receipt, and the route
-- and a process can be lost in any window between them. What the next tick
comes back to is a checkout on the replay and a comment that got as far as it
got, and the whole of what these cases pin is that each of those states
resolves into exactly one finish.

Two readings decide it. The REMOTE says which effect the dead tick reached:
still on the anchor and the push never went out, on the replay and it did,
anywhere else and somebody moved the branch. The pinned comment says which of
the transfer's own writes it reached. Neither is enough alone -- a remote
carrying the replay with the permission still outstanding is a receipt this
tick owes, and the same remote past that receipt is a route to finish and
nothing more.

What none of them may do is start over. A replay of the exact change a human
already ruled on must not spawn an agent, take a measurement, rebase again,
force-rewrite a branch the remote already has, or put a second adjudication on
the thread -- and every state nobody can vouch for is fail-closed: the branch
goes back onto the anchor, or the anchor stays pinned, and a human is asked.
"""
from __future__ import annotations

import itertools
import unittest
from types import MappingProxyType
from unittest.mock import MagicMock, patch

from orchestrator import config
from orchestrator.git import branch_transport as _branch_transport
from orchestrator.git.base_sync import transfers
from orchestrator.git.base_sync.models import _AutoRebaseRecoveryContext
from orchestrator.workflow.late_split import (
    exemption as _exemption,
    rewrites as _rewrites,
)
from orchestrator.workflow.stages.implementing import (
    late_reconcile as _reconcile,
)
from orchestrator.workflow.state import WorkflowLabel
from tests.git.base_sync.exemption_test_support import (
    EVENT_MEASUREMENT,
    EVENT_TRANSFER,
    LEASE,
    RECOVERY_PUSHED,
    RECOVERY_RELABELLED,
    REPLAYED_BASE_SHA,
    REVISION,
    _CleanRebaseCase,
    adjudicated,
)
from tests.git.base_sync.refresh_scenarios import PUSH_PATCH
from tests.git.base_sync.refresh_test_support import (
    AFTER_SHA,
    BEFORE_SHA,
    EVENT_BASE_REBASED,
    HARD_RESET_FLAG,
    ISSUE,
    KEY_PARK_REASON,
    KEY_PENDING_PUSH_SHA,
    KEY_REVIEW_ROUND,
    LABEL_IN_REVIEW as LABEL,
    LABEL_VALIDATING,
    METHOD_FIELD,
    PARK_FAILED,
    PARK_PUSH_FAILED,
    PR_NUMBER,
    RESET_COMMAND,
    FakePRRef,
    _patched,
)
from tests.workflow.fixtures import LABEL_DECOMPOSING

# A commit neither this issue nor its recovery put on the branch, which is
# what an out-of-band update to the pull request looks like from here.
FOREIGN_SHA = "f0000000" * 5

# The field a hand edit takes out of an authorization group, leaving a claim
# about this issue's exemption that nothing can read back whole.
DAMAGED_FIELD = "late_rewrite_to_base_sha"

# The leftovers that make a checkout something no contribution may be
# fingerprinted beside.
LOOSE_EDITS = ("scratch.txt",)

# The scenario alias the hardened git seam is installed under, which is what
# a reset that never happened is read back off.
HARDENED_PATCH = "hardened"

# The receipt a landed publication leaves, both halves: the commit that
# reached the remote and the head it was pushed FROM, which is what dates it
# to the attempt this recovery is finishing.
KEY_PUBLISHED_SHA = "implementing_published_sha"
KEY_PUBLISHED_LEASE = "implementing_published_lease"

# What the attempt recorded about its own replay, dropped by the same write
# that drops the anchor beside it: the head it produced, and the publication
# it produced it for.
KEY_PENDING_REWRITE_SHA = "pending_auto_base_rebase_rewrite_sha"
KEY_PENDING_REWRITE_PR = "pending_auto_base_rebase_rewrite_pr"
KEY_PENDING_REWRITE_STAGE = "pending_auto_base_rebase_rewrite_stage"
KEY_ANNOUNCED_SHA = "pending_auto_base_rebase_announced_sha"

# The whole of that record, for the case that seeds the attempt which
# left nothing but its anchor behind.
ATTEMPT_RECORD_KEYS = (
    KEY_PENDING_REWRITE_SHA,
    KEY_PENDING_REWRITE_PR,
    KEY_PENDING_REWRITE_STAGE,
    KEY_ANNOUNCED_SHA,
)

# A second open pull request on the same branch, for the case where the issue
# is repointed at one the interrupted rewrite was never made against.
REPOINTED_PR_NUMBER = 43

# The field a hand edit takes out of the identity group, leaving an exemption
# whose contribution nothing can name.
DAMAGED_IDENTITY_FIELD = "late_exempt_fingerprint"

# The whole of that group: what a case seeding the legacy shape -- an
# exemption with nothing beside it naming what it contributes -- takes off.
IDENTITY_KEYS = (
    DAMAGED_IDENTITY_FIELD,
    "late_exempt_fingerprint_format",
    "late_exempt_base_sha",
    "late_exempt_candidate_sha",
)

# A lease no reader can hold to a commit, and one naming a head this attempt
# was never pinned to.
MALFORMED_LEASE = "not-a-commit"

# The pinned field an issue's own publication is recorded under, and what a
# client that cannot answer for the issue raises.
KEY_PR_NUMBER = "pr_number"

# The debt a grant writes beside the permission, in the one statement that
# makes each answer for the other.
KEY_APPROVED_SHA = "late_approved_sha"

# What a pull request that has been merged reads back as.
PR_STATE_CLOSED = "closed"

# The three terms of an authorization a recovery cross-binds to the attempt it
# is finishing: the head the push was leased against, the pull request it was
# made for, and the base the replay was read over.
KEY_REWRITE_LEASE = "late_rewrite_lease"
KEY_REWRITE_PR = "late_rewrite_pr_number"
KEY_REWRITE_TO_BASE = "late_rewrite_to_base_sha"
GET_ISSUE = "get_issue"
UNREADABLE_ISSUE = "the issue could not be read again"

# The pull-request read the refresh takes before it will run a recovery, and
# the round the reviewer had already spent on the head the rebase replaced.
GET_PR = "get_pr"
UNREADABLE_PR = "the pull request could not be read"
PUSH_BRANCH = "_push_branch"
SPENT_ROUNDS = 3

# The two windows an interrupted attempt leaves with no debt and no count on
# the comment: one before anything was measured, and one past the receipt
# whose settlement cleared the debt it had. Both leave the anchor.
_NO_DEBT_SEAMS = MappingProxyType({
    "before the transfer was granted": "_crashes_before_the_grant",
    "past the receipt that settled it": "_crashes_before_the_route",
})

# The method and the stage the tick that really published recorded itself
# under, which is the one record a resumed finish may not add to.
CLEAN_REBASE = "auto_clean_rebase"
STAGE_FIELD = "stage"

# The kill switch a whole decomposition run is behind, which an operator may
# have flipped off in the window a recovery is coming back from.
CONFIG_DECOMPOSE = "DECOMPOSE"

# The scenario alias the rebase seam is installed under, read back where a
# recovery's answer decides whether a second rebase still runs.
REBASE_PATCH = "rebase"

# The permission group as it stands before the push that spends it, and the
# stage a hand moves an issue to while the process is down.
KEY_REWRITE_PHASE = "late_rewrite_phase"
PHASE_AUTHORIZED = "authorized"

# The note a settled transfer keeps until its record is out, and a value for
# it that names no reading this build knows.
KEY_REWRITE_PROOF = "late_rewrite_proof"
DAMAGED_PROOF = "not-a-reading"

# The reply an operator leaves once they have repaired whatever a park was
# about, and the two fields the retry it releases has to spend.
HUMAN_LOGIN = "human"
RETRY_COMMENT_ID = 4100
RETRY_BODY = "worktree cleaned up, please retry"
KEY_AWAITING_HUMAN = "awaiting_human"
KEY_LAST_ACTION_COMMENT_ID = "last_action_comment_id"

# What a real replay looks like to the divergence probe: one commit of
# its own, and the pull request's head on no local history at all.
REBASED_COUNTS = (1, 2)


class _ReadableOnce:
    """A client that answers for the issue once and refuses after that.

    The refresh reads the issue to find the worktree it is about, and the
    permit re-reads it before it will carry a human's verdict anywhere. Only
    the second read is what a case about an unconfirmable owner is seeding, so
    the first is left alone rather than the whole client being taken away.
    """

    def __init__(self, readable) -> None:
        self._readable = readable
        self._reads = itertools.count()

    def __call__(self, number: int):
        """Answer the tick's own read, and refuse every one behind it."""
        if next(self._reads):
            raise RuntimeError(UNREADABLE_ISSUE)
        return self._readable(number)


class _ResumedRebaseCase(_CleanRebaseCase):
    """One adjudicated rebase stopped mid-tick and resumed on the next one."""

    def setUp(self) -> None:
        super().setUp()
        adjudicated(self)

    def _assert_finished_the_route(self, method: str) -> None:
        """The anchor is gone, the round is reset, and review has the head."""
        self._assert_anchor(None)
        self.assertEqual(self._pinned()[KEY_REVIEW_ROUND], 0)
        self._assert_routed(True)
        rebased = self._events_of(EVENT_BASE_REBASED)
        self.assertEqual(rebased[-1][METHOD_FIELD], method)

    def _assert_settled_once(self) -> None:
        """The verdict is on the replay, and it was moved exactly once."""
        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, AFTER_SHA))
        self.assertEqual(
            _rewrites.read_rewrite_authorization(durable).phase,
            _rewrites.LateRewritePhase.PUBLISHED,
        )
        self.assertEqual(len(self._events_of(EVENT_TRANSFER)), 1)

    def _issue(self):
        """The issue this fixture's whole world is about."""
        return self.gh._issues[ISSUE]

    def _edited(self, edit) -> None:
        """Apply one hand edit to the pinned comment, durably."""
        issue = self._issue()
        state = self.gh.read_pinned_state(issue)
        edit(state)
        self.gh.write_pinned_state(issue, state)

    def _assert_anchor(self, expected) -> None:
        """What the pinned comment says this attempt is still owed, if any."""
        self.assertEqual(self._pinned()[KEY_PENDING_PUSH_SHA], expected)

    def _assert_routed(self, routed: bool) -> None:
        """Whether the reviewer was sent back to the rewritten head."""
        self.assertEqual(
            (ISSUE, LABEL_VALIDATING) in self.gh.label_history, routed,
        )

    def _assert_parked(self, reason: str) -> None:
        """The reason a human is being asked to look at this issue."""
        self.assertEqual(self._pinned()[KEY_PARK_REASON], reason)

    def _assert_nothing_left(self, resumed) -> None:
        """No push went out on the road this tick could not finish."""
        resumed[PUSH_PATCH].assert_not_called()

    def _pinned(self) -> dict:
        """The pinned comment as the fake client stores it."""
        return self.gh.pinned_data(ISSUE)

    def _assert_reset_once(self, resumed) -> None:
        """The branch was put back onto the anchor exactly once."""
        self.assertEqual(len(self._resets_of(resumed)), 1)

    def _resets_of(self, resumed) -> list:
        """Every hard reset the hardened git seam was asked for this tick."""
        return [
            recorded for recorded in resumed[HARDENED_PATCH].call_args_list
            if recorded.args[:2] == (RESET_COMMAND, HARD_RESET_FLAG)
        ]

    def _assert_nothing_readjudicated(self) -> None:
        """No agent, no reading, and no second question for a human."""
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])
        self.assertNotIn((ISSUE, LABEL_DECOMPOSING), self.gh.label_history)


class ResumedHandoffTest(_ResumedRebaseCase, unittest.TestCase):
    """Each window one transfer has, and the single finish it resolves into.

    One class rather than four because the outcome is one outcome -- the
    verdict on the replay, the reviewer routed to it, and nobody asked to
    adjudicate the change again -- and what differs is only where the process
    stopped and what that leaves the next tick to do.
    """

    def test_an_ungranted_rewrite_is_re_derived(self) -> None:
        # The replay is on the branch and the comment says nothing about it,
        # so the record cannot supply the evidence and a recovery that asked
        # it would measure an adjudicated change afresh. What it assembles
        # instead is the same four readings the dead tick would have taken,
        # and the same permit rules on them: the push is named against the
        # replay and pinned to the anchor the remote is still standing on.
        self._crashes_before_the_grant()
        crashed = self._durable()

        resumed = self._resumes()

        self.assertFalse(_rewrites.carries_rewrite_authorization(crashed))
        self.assertEqual(crashed.get(KEY_PENDING_PUSH_SHA), BEFORE_SHA)
        pushed = resumed[PUSH_PATCH].call_args.kwargs
        self.assertEqual(pushed[REVISION], AFTER_SHA)
        self.assertEqual(pushed[LEASE], BEFORE_SHA)
        self._assert_settled_once()
        self._assert_nothing_readjudicated()
        self._assert_finished_the_route(RECOVERY_PUSHED)

    def test_an_outstanding_permission_is_spent(self) -> None:
        # The permit is re-asked over the record the grant left -- the
        # recovery has no evidence of its own -- and the receipt behind the
        # reissued push is what finally carries the verdict over. The debt
        # that grant recorded is what freezes this branch out of the very
        # recovery the anchor beside it exists for, so leaving it there is
        # how a later stage lands the push with the reviewer never routed.
        self._crashes_before_the_push()

        resumed = self._resumes()

        pushed = resumed[PUSH_PATCH].call_args.kwargs
        self.assertEqual(pushed[REVISION], AFTER_SHA)
        self.assertEqual(pushed[LEASE], BEFORE_SHA)
        self._assert_settled_once()
        self._assert_nothing_readjudicated()
        self._assert_finished_the_route(RECOVERY_PUSHED)

    def test_a_landed_push_earns_a_leased_no_op(self) -> None:
        # The request went out and the process died waiting for its answer,
        # so the pull request carries the replay while the comment still says
        # a push is owed for it. The remote standing there already makes the
        # push the leased no-op that proves it -- named against that commit
        # and pinned to it, a request with nothing to send rather than a
        # second force-rewrite of a branch the pull request has.
        self._crashes_before_the_push()

        resumed = self._resumes(remote_head=AFTER_SHA)

        pushed = resumed[PUSH_PATCH].call_args.kwargs
        self.assertEqual(pushed[REVISION], AFTER_SHA)
        self.assertEqual(pushed[LEASE], AFTER_SHA)
        self._assert_settled_once()
        self.assertEqual(
            self._events_of(EVENT_TRANSFER)[0]["transfer_proof"],
            str(_rewrites.LateRewriteProof.ALREADY_PUBLISHED),
        )
        self._assert_nothing_readjudicated()
        self._assert_finished_the_route(RECOVERY_RELABELLED)

    def test_a_settled_transfer_owes_only_its_route(self) -> None:
        # The receipt landed with the rotation on it, so every question this
        # recovery could ask is already answered: no permission left
        # outstanding, nothing to send, and nothing to report twice.
        self._crashes_before_the_route()

        resumed = self._resumes(remote_head=AFTER_SHA)

        resumed[PUSH_PATCH].assert_not_called()
        self._assert_settled_once()
        self._assert_nothing_readjudicated()
        self._assert_finished_the_route(RECOVERY_RELABELLED)


class FailClosedRecoveryTest(_ResumedRebaseCase, unittest.TestCase):
    """Every state nobody can vouch for parks or resets rather than acts."""

    def test_a_moved_remote_rolls_the_permission_back(self) -> None:
        # Somebody pushed to the branch while the interrupted tick was down,
        # so the replay may not be published over them. The reset puts the
        # branch back on the commit the exemption never left, and the
        # permission goes with the object no branch has any more.
        self._crashes_before_the_push()

        self._resumes(remote_head=FOREIGN_SHA, diverged=(1, 1))

        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, BEFORE_SHA))
        self.assertFalse(_rewrites.carries_rewrite_authorization(durable))
        self.assertEqual(
            self._pinned()[KEY_PARK_REASON], PARK_PUSH_FAILED,
        )

    def test_an_unreadable_permission_holds_the_route(self) -> None:
        # A group this build cannot read back whole is the only account there
        # is of how the exemption came to name what it names. Finishing the
        # route over it would clear the one anchor that brings this recovery
        # back, leaving the replay to be measured afresh, so the tick parks
        # with the record exactly as it stands.
        self._crashes_before_the_push()
        self._damages_the_permission()

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        self._assert_standing_permission()
        self._assert_held_for_a_human(resumed)

    def test_a_loose_checkout_holds_the_route(self) -> None:
        # A contribution fingerprinted beside changes nobody committed is not
        # the one the pull request carries, so the settlement may not be
        # taken -- and the route may not be finished either, since the
        # exemption is still on the commit the adjudication accepted.
        self._crashes_before_the_push()

        resumed = self._resumes(remote_head=AFTER_SHA, dirty=LOOSE_EDITS)

        self._assert_nothing_left(resumed)
        self._assert_standing_permission()
        self._assert_held_for_a_human(resumed)

    def test_a_refused_no_op_parks_in_place(self) -> None:
        # The pull request was standing on the replay when this tick read it
        # and refused the lease a moment later, so the remote moved in
        # between. The checkout is on the commit the pull request was
        # carrying, so nothing is reset off it and the anchor stays pinned for
        # the next tick to classify the remote afresh.
        self._crashes_before_the_push()

        resumed = self._resumes(remote_head=AFTER_SHA, push=False)

        self._assert_anchor(BEFORE_SHA)
        self._assert_parked(PARK_PUSH_FAILED)
        self.assertEqual(self._resets_of(resumed), [])
        self.assertEqual(self._events_of(EVENT_TRANSFER), [])

    def test_a_landed_rewrite_nothing_explains_holds(self) -> None:
        # The replay is on the pull request and the comment says nothing at
        # all about it -- no permission, no receipt. Finishing the route would
        # clear the anchor over an exemption still naming the commit the
        # adjudication accepted, so the next tick would measure the replay as
        # a fresh candidate.
        self._crashes_before_the_grant()

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, BEFORE_SHA))
        self.assertFalse(_rewrites.carries_rewrite_authorization(durable))
        self.assertEqual(self._events_of(EVENT_TRANSFER), [])
        self._assert_held_for_a_human(resumed)

    def test_a_settled_transfer_with_no_receipt_holds(self) -> None:
        # The rotation and the receipt go down in one write, so a comment
        # claiming the first without the second is one that did not land
        # whole -- and this tick may not decide which half is true.
        self._crashes_before_the_route()
        self._forgets_the_receipt()

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        self._assert_held_for_a_human(resumed)

    def test_a_partial_identity_holds_a_landing(self) -> None:
        # The exemption is real and what it contributes is not readable, so
        # the fail-closed readers answer "no identity" -- the same answer an
        # issue that never earned a verdict gives. Finishing the route on it
        # would drop the anchor with the verdict still on the old commit.
        self._crashes_before_the_grant()
        self._damages_the_identity()

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        self._assert_held_for_a_human(resumed)

    def test_a_partial_identity_resets_a_replay(self) -> None:
        # The same damage on the road where nothing has been pushed yet. Left
        # to the ordinary gate the replay is measured past the same ceiling
        # and adjudicated again, so the branch goes back onto the anchor and
        # a human is asked about the record instead.
        self._crashes_before_the_grant()
        self._damages_the_identity()

        resumed = self._resumes()

        self._assert_nothing_left(resumed)
        self._assert_reset_and_parked(resumed)

    def test_a_damaged_permission_resets_a_replay(self) -> None:
        # A transfer group this build cannot read whole, over a branch the
        # crash left rebased and unpushed. The permit refuses it, so the only
        # road left is the measurement -- which is the one answer an
        # adjudicated change may not get.
        self._crashes_before_the_push()
        self._damages_the_permission()

        resumed = self._resumes()

        self._assert_nothing_left(resumed)
        self._assert_reset_and_parked(resumed)

    def test_a_receipt_with_no_lease_holds(self) -> None:
        # A receipt is never cleared, so the commit it names on its own
        # vouches for any pull request somebody rewound onto it.
        self._assert_undatable_receipt_holds(None)

    def test_a_malformed_receipt_lease_holds(self) -> None:
        # A value that is not a commit dates the receipt to nothing, and the
        # reader may not fall back to the half it can still read.
        self._assert_undatable_receipt_holds(MALFORMED_LEASE)

    def test_a_mismatched_receipt_lease_holds(self) -> None:
        # A whole commit that is not the anchor this recovery holds records a
        # push made from some other attempt.
        self._assert_undatable_receipt_holds(FOREIGN_SHA)

    def _damages_the_permission(self) -> None:
        """Take one field out of the group the grant left on the comment."""
        issue = self._issue()
        state = self.gh.read_pinned_state(issue)
        state.data.pop(DAMAGED_FIELD)
        self.gh.write_pinned_state(issue, state)

    def _assert_undatable_receipt_holds(self, lease) -> None:
        """A settled transfer whose receipt this attempt cannot date holds."""
        self._crashes_before_the_route()
        self._repoints_the_receipt(lease)

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        self._assert_held_for_a_human(resumed)

    def _damages_the_identity(self) -> None:
        """Take one field out of the record of what the exempt commit adds."""
        self._edited(lambda state: state.data.pop(DAMAGED_IDENTITY_FIELD))

    def _repoints_the_receipt(self, lease) -> None:
        """Leave the receipt naming a head this attempt was not pushed from."""
        self._edited(lambda state: state.set(KEY_PUBLISHED_LEASE, lease))

    def _assert_reset_and_parked(self, resumed) -> None:
        """The branch is back on the anchor and a human owns the record."""
        self._assert_reset_once(resumed)
        self._assert_nothing_readjudicated()
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self._assert_anchor(None)
        pinned = self._pinned()
        self.assertIsNone(pinned[KEY_PENDING_REWRITE_SHA])
        self.assertEqual(pinned[KEY_PARK_REASON], PARK_FAILED)
        self._assert_routed(False)

    def _assert_standing_permission(self) -> None:
        """The verdict is where the adjudication put it, and the claim stands."""
        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, BEFORE_SHA))
        self.assertTrue(_rewrites.carries_rewrite_authorization(durable))
        self.assertEqual(self._events_of(EVENT_TRANSFER), [])

    def _forgets_the_receipt(self) -> None:
        """Take the record of what reached the remote off the comment."""
        issue = self._issue()
        state = self.gh.read_pinned_state(issue)
        state.set(KEY_PUBLISHED_SHA, None)
        self.gh.write_pinned_state(issue, state)

    def _assert_held_for_a_human(self, resumed) -> None:
        """The anchor stands, HEAD is where it was, and nothing was reported.

        The remote carries the replay, so nothing is reset off it -- the park
        is what a route this tick could not finish owes, and the anchor left
        pinned is what lets the next one classify the remote afresh.
        """
        self._assert_nothing_readjudicated()
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self.assertEqual(self._resets_of(resumed), [])
        self._assert_anchor(BEFORE_SHA)
        self._assert_parked(PARK_PUSH_FAILED)
        self._assert_routed(False)


if __name__ == "__main__":
    unittest.main()


class RolledBackRemoteTest(_ResumedRebaseCase, unittest.TestCase):
    """The remote somebody put back after this attempt's push had landed."""

    def test_the_rollback_is_answered_not_undone(self) -> None:
        # The pull request is back on the head the rebase found it on, which
        # is the very commit a reissued push would lease itself against -- so
        # the lease would be satisfied and the rollback gone. Nothing is
        # pushed for it. What the externally moved remote earns instead is
        # HEAD onto the anchor the pull request is standing on, the anchor
        # dropped with it, and a human asked which of the two heads the branch
        # is supposed to be on. The transfer itself is left alone: the write
        # that receipted it moved the exemption, and a rollback nobody here
        # made is not this recovery's to undo.
        self._crashes_before_the_route()

        resumed = self._resumes(remote_head=BEFORE_SHA, diverged=(1, 1))

        resumed[PUSH_PATCH].assert_not_called()
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self._assert_routed(False)
        self._assert_reset_once(resumed)
        pinned = self._pinned()
        self.assertIsNone(pinned[KEY_PENDING_PUSH_SHA])
        self.assertEqual(pinned[KEY_PARK_REASON], PARK_PUSH_FAILED)
        self._assert_settled_once()


class UnprovenLandingTest(_ResumedRebaseCase, unittest.TestCase):
    """A remote and a checkout that agree on a commit nothing recorded."""

    def test_a_mismatched_record_holds_the_route(self) -> None:
        # Both refs moved together while the pending record still names the
        # replay this attempt made. They agreeing proves only that they agree.
        self._crashes_before_the_route()
        self._repoints_the_rewrite(FOREIGN_SHA)

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        self._assert_held_for_a_human(resumed)

    def test_a_malformed_record_holds_the_route(self) -> None:
        # A pull request number no reader can hold to an identity leaves the
        # record unreadable as a whole, and an unreadable record vouches for
        # no publication at all.
        self._crashes_before_the_route()
        self._edited(lambda state: state.set(KEY_PENDING_REWRITE_PR, "forty"))

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        self._assert_held_for_a_human(resumed)

    def _repoints_the_rewrite(self, sha: str) -> None:
        """Leave the pending record naming a replay this attempt never made."""
        self._edited(lambda state: state.set(KEY_PENDING_REWRITE_SHA, sha))

    def _assert_held_for_a_human(self, resumed) -> None:
        """The anchor stands, HEAD is where it was, and the route is unfinished."""
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self._assert_anchor(BEFORE_SHA)
        self._assert_parked(PARK_PUSH_FAILED)
        self._assert_routed(False)


class RefusedSettlementTest(_ResumedRebaseCase, unittest.TestCase):
    """The permit is the whole of what may settle a landed rewrite."""

    def test_a_relabelled_issue_refuses(self) -> None:
        # The rewrite was entered from the stage the record names, and a
        # publication made under one stage may not be settled under another.
        self._crashes_before_the_push()
        self.gh.set_workflow_label(
            self._issue(), WorkflowLabel.DOCUMENTING,
        )

        self._assert_refuses()

    def test_a_repointed_publication_refuses(self) -> None:
        # The issue now records some other pull request, so the permission on
        # the comment describes a publication this call is not entered on.
        self._crashes_before_the_push()
        self._repoints_the_pull_request()

        self._assert_refuses()

    def test_an_unreadable_owner_refuses(self) -> None:
        # A transfer carries a human's verdict forward without re-asking a
        # human anything, so the issue is re-read for it -- and a read that
        # established nothing settles nothing.
        self._crashes_before_the_push()
        self._unreadable_after_the_tick_opens()

        self._assert_refuses()

    def test_an_unheld_lease_refuses(self) -> None:
        # The head the push was leased against is the one end nothing else
        # here reads as an object, and an id this host cannot peel is evidence
        # nobody can check.
        self._crashes_before_the_push()
        self.reading.unheld.add(BEFORE_SHA)

        self._assert_refuses()

    def _repoints_the_pull_request(self) -> None:
        """Record a different open pull request on the same branch."""
        self._add_pr(
            pr_number=REPOINTED_PR_NUMBER,
            head=FakePRRef(sha=AFTER_SHA),
        )
        issue = self._issue()
        state = self.gh.read_pinned_state(issue)
        state.set(KEY_PR_NUMBER, REPOINTED_PR_NUMBER)
        self.gh.write_pinned_state(issue, state)

    def _unreadable_after_the_tick_opens(self) -> None:
        """Let the refresh find the issue and refuse every read behind it."""
        _patched(self, self.gh, GET_ISSUE, _ReadableOnce(self.gh.get_issue))

    def _assert_refuses(self) -> None:
        """Nothing is pushed, nothing rotates, and the route is unfinished."""
        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, BEFORE_SHA))
        self.assertEqual(
            _rewrites.read_rewrite_authorization(durable).phase,
            _rewrites.LateRewritePhase.AUTHORIZED,
        )
        self.assertEqual(self._events_of(EVENT_TRANSFER), [])
        self._assert_nothing_readjudicated()
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self._assert_anchor(BEFORE_SHA)


class ForeignPublicationTest(_ResumedRebaseCase, unittest.TestCase):
    """An attempt made for a publication this issue no longer records."""

    def test_a_relabel_parks_an_unpushed_replay(self) -> None:
        # Every road out of a recovery posts a notice to the pull request this
        # tick holds and files its audit event under the stage this tick
        # reads. A relabel made while the process was down would have both
        # attributed to a publication the attempt was never made for.
        self._crashes_before_the_grant()
        self._relabels()

        resumed = self._resumes()

        self._assert_parked_in_place(resumed)
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])

    def test_a_repoint_parks_a_landed_rewrite(self) -> None:
        # The same after the receipt has landed, which is the road that would
        # otherwise finalize: the transfer is settled and nothing is left to
        # push, so the only thing finishing buys is a notice and an event on
        # the wrong pull request -- and the anchor gone.
        self._crashes_before_the_route()
        self._repoints_the_pull_request()

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_parked_in_place(resumed)
        self.assertEqual(len(self._events_of(EVENT_TRANSFER)), 1)

    def test_the_evidence_names_the_recorded_terms(self) -> None:
        # The record is the source of the terms a re-derived rewrite is
        # decided on. Taken from the context instead they would compare today
        # with today, and the permit's publication checks would pass on any
        # repoint or relabel the crash window allowed.
        self._crashes_before_the_grant()

        rewrite = transfers._reconstructed(
            self._elsewhere(), AFTER_SHA, transfers._Handoff.UNRECORDED,
        )

        self.assertEqual(rewrite.pr_number, PR_NUMBER)
        self.assertEqual(rewrite.source_stage, WorkflowLabel.IN_REVIEW)

    def _relabels(self) -> None:
        """Move the issue to another stage the refresh also drives."""
        self.gh.set_workflow_label(
            self._issue(), WorkflowLabel.DOCUMENTING,
        )

    def _repoints_the_pull_request(self) -> None:
        """Record a different open pull request on the same branch."""
        self._add_pr(
            pr_number=REPOINTED_PR_NUMBER,
            head=FakePRRef(sha=AFTER_SHA),
        )
        issue = self._issue()
        state = self.gh.read_pinned_state(issue)
        state.set(KEY_PR_NUMBER, REPOINTED_PR_NUMBER)
        self.gh.write_pinned_state(issue, state)

    def _elsewhere(self) -> _AutoRebaseRecoveryContext:
        """The same recovery, on the publication a repoint moved it to."""
        durable = self._durable()
        return _AutoRebaseRecoveryContext(
            gh=self.gh,
            spec=self.spec,
            issue=self._issue(),
            state=durable,
            worktree=self.wt,
            pr_number=REPOINTED_PR_NUMBER,
            label=WorkflowLabel.DOCUMENTING,
            pending_pre_rebase_sha=BEFORE_SHA,
            pending_rewrite=transfers._pending_rewrite(durable),
        )

    def _assert_parked_in_place(self, resumed) -> None:
        """Nothing pushed, nothing reset, and the whole record still pinned."""
        self._assert_nothing_left(resumed)
        self.assertEqual(self._resets_of(resumed), [])
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self._assert_nothing_readjudicated()
        self._assert_anchor(BEFORE_SHA)
        pinned = self._pinned()
        self.assertEqual(pinned[KEY_PENDING_REWRITE_SHA], AFTER_SHA)
        self.assertEqual(pinned[KEY_PARK_REASON], PARK_FAILED)


class ForeignRelabelTest(_ResumedRebaseCase, unittest.TestCase):
    """A move to the stage this route ends on, made by somebody else."""

    def test_a_relabel_over_an_unpushed_replay_parks(self) -> None:
        # The label alone cannot say whose move it was, and the effect a
        # finish leaves is absent here: nothing was pushed, so the pull
        # request is still on the anchor and no receipt names the replay.
        # Taken for this route's own step, the tick would measure and publish
        # a checkout nothing vouched for.
        self._crashes_before_the_grant()
        self.gh.set_workflow_label(self._issue(), WorkflowLabel.VALIDATING)

        resumed = self._resumes()

        self._assert_nothing_left(resumed)
        self.assertEqual(self._resets_of(resumed), [])
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])
        self._assert_anchor(BEFORE_SHA)
        self._assert_parked(PARK_FAILED)


class RefusedRetryTest(_ResumedRebaseCase, unittest.TestCase):
    """The permit is the whole of what may reissue an interrupted push."""

    def test_an_unheld_lease_resets_the_replay(self) -> None:
        # The head the push was leased against is not an object this host
        # holds, so the permission granted for the replay cannot be re-asked
        # on evidence anybody can check. Measured instead, the replay is
        # force-pushed and the recovery cleared with the verdict unmoved.
        self._crashes_before_the_push()
        self.reading.unheld.add(BEFORE_SHA)

        resumed = self._resumes()

        self._assert_nothing_left(resumed)
        self._assert_reset_once(resumed)
        self.assertEqual(self._events_of(EVENT_TRANSFER), [])
        self._assert_nothing_readjudicated()
        self._assert_anchor(None)
        self._assert_parked(PARK_FAILED)
        self.assertTrue(_exemption.is_exempt(self._durable(), BEFORE_SHA))


class UnboundAuthorizationTest(_ResumedRebaseCase, unittest.TestCase):
    """A whole record whose terms belong to some other attempt."""

    def test_a_foreign_lease_holds_the_route(self) -> None:
        # The head the permit was granted against is not the anchor this
        # recovery is finishing, so the permission describes a push some
        # other attempt was going to make.
        self._assert_unbound(KEY_REWRITE_LEASE, FOREIGN_SHA)

    def test_a_foreign_publication_holds_the_route(self) -> None:
        # The pull request the rewrite was made against is not the one the
        # attempt recorded rebasing for.
        self._assert_unbound(KEY_REWRITE_PR, REPOINTED_PR_NUMBER)

    def test_a_foreign_replayed_base_holds_the_route(self) -> None:
        # The base the transfer says the replay sits over is not the one the
        # identity beside it records, so the pair the digest was taken
        # between is a contribution this issue never adjudicated.
        self._assert_unbound(KEY_REWRITE_TO_BASE, FOREIGN_SHA)

    def _assert_unbound(self, field: str, recorded) -> None:
        """One term moved off the attempt, and the hold it has to earn."""
        self._crashes_before_the_route()
        self._edited(lambda state: state.set(field, recorded))

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self._assert_anchor(BEFORE_SHA)
        self._assert_parked(PARK_PUSH_FAILED)


class LooseSettledTreeTest(_ResumedRebaseCase, unittest.TestCase):
    """A settled handoff over a checkout carrying work nobody committed."""

    def test_a_dirty_settled_handoff_holds_the_route(self) -> None:
        # Finishing hands the issue to the reviewer, and a reviewer sent to a
        # checkout with uncommitted files reads work the pull request does not
        # have as though it were under review.
        self._crashes_before_the_route()

        resumed = self._resumes(remote_head=AFTER_SHA, dirty=LOOSE_EDITS)

        self._assert_nothing_left(resumed)
        self.assertEqual(self._resets_of(resumed), [])
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self._assert_routed(False)
        self._assert_anchor(BEFORE_SHA)
        self._assert_parked(PARK_PUSH_FAILED)


class UnroutedFinishTest(_ResumedRebaseCase, unittest.TestCase):
    """A finish that said what it published and never routed the reviewer."""

    def test_the_route_is_made_and_not_repeated(self) -> None:
        # The anchor is the only thing that brings this tick back, so clearing
        # it without the relabel would strand the issue on the stage the
        # rebase ran from with nothing left to correct it. What the mark
        # beside it buys is the other half: the notice and the audit event
        # are already out, so this tick owes the route and the write alone.
        self._crashes_at_the_relabel()

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_routed(True)
        self._assert_anchor(None)
        self.assertEqual(self._pinned()[KEY_REVIEW_ROUND], 0)
        self._assert_nothing_left(resumed)
        self.assertEqual(
            [record[METHOD_FIELD] for record in self._events_of(
                EVENT_BASE_REBASED,
            )],
            [CLEAN_REBASE],
        )
        self.assertEqual(len(self._events_of(EVENT_TRANSFER)), 1)

    def test_a_retried_finish_spends_the_reply(self) -> None:
        # The same window, reached the long way round: the announced attempt
        # was parked over a checkout carrying loose work, a human cleaned it
        # up and replied, and the retry is what re-enters this route. The
        # finish it makes owes that reply -- routed to `validating` with the
        # park still flagged and the watermark still behind the comment, the
        # stage below stands down on a reason it does not own, and the anchor
        # that would bring anything back is already gone.
        self._crashes_at_the_relabel()
        self._parks_over_a_loose_tree()
        self._add_comment(RETRY_COMMENT_ID, RETRY_BODY, HUMAN_LOGIN)

        self._resumes(remote_head=AFTER_SHA)

        self._assert_routed(True)
        self._assert_anchor(None)
        pinned = self._pinned()
        self.assertFalse(pinned[KEY_AWAITING_HUMAN])
        self.assertIsNone(pinned[KEY_PARK_REASON])
        self.assertEqual(pinned[KEY_LAST_ACTION_COMMENT_ID], RETRY_COMMENT_ID)

    def _parks_over_a_loose_tree(self) -> None:
        """Hold the announced finish for a human, with the record intact."""
        self._resumes(remote_head=AFTER_SHA, dirty=LOOSE_EDITS)
        self._assert_parked(PARK_PUSH_FAILED)


class UndoneAttemptTest(_ResumedRebaseCase, unittest.TestCase):
    """A branch put back on the anchor with the attempt's records standing."""

    def test_an_undone_replay_parks_and_rolls_back(self) -> None:
        # HEAD is exactly where the attempt anchored it and the comment still
        # carries the replay it recorded and the permission granted for it.
        # Read as an attempt that never started, the anchor would be dropped,
        # the transfer state left for the next grant to trip over, and the
        # branch handed straight to a fresh rebase.
        self._crashes_before_the_push()

        resumed = self._resumes(local_head=BEFORE_SHA)

        self._assert_nothing_left(resumed)
        self._assert_reset_once(resumed)
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])
        self._assert_routed(False)
        self._assert_anchor(None)
        self._assert_parked(PARK_FAILED)

    def test_the_rollback_it_abandoned_is_finished(self) -> None:
        # The reset that put the branch back owed two drops and made neither:
        # the debt for a commit no branch has, and the permission that will
        # never be spent on it. The exemption is untouched, since the grant
        # never moved it.
        self._crashes_before_the_push()

        self._resumes(local_head=BEFORE_SHA)

        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, BEFORE_SHA))
        self.assertFalse(_rewrites.carries_rewrite_authorization(durable))
        self.assertIsNone(self._pinned()[KEY_APPROVED_SHA])
        self.assertIsNone(self._pinned()[KEY_PENDING_REWRITE_SHA])


class DamagedAttemptRecordTest(_ResumedRebaseCase, unittest.TestCase):
    """A record of the attempt that claims more than it can show."""

    def test_lost_terms_reset_the_replay(self) -> None:
        # The head stands and the publication it was made for is gone, which
        # is an order no write here produces. Read as the in-flight window it
        # half resembles, the permit would be asked over terms this tick made
        # up out of whatever the issue says now.
        self._crashes_before_the_grant()
        self._edited(
            lambda state: state.set(KEY_PENDING_REWRITE_PR, None),
        )

        resumed = self._resumes()

        self._assert_reset_and_parked(resumed)

    def test_a_malformed_head_resets_the_replay(self) -> None:
        # A value that is not a whole git object id is not a commit this tick
        # can compare anything to. Accepted for being a string, it would name
        # a checkout nothing here ever wrote and reach the same push.
        self._assert_resets_the_replay(MALFORMED_LEASE)

    def test_a_malformed_head_is_no_record(self) -> None:
        # Read one step earlier than the park: a value that is not a whole git
        # object id is not a commit anything can be compared to, so the group
        # is damaged rather than one naming a head the checkout might turn out
        # to be standing on.
        self._crashes_before_the_grant()
        self._edited(
            lambda state: state.set(KEY_PENDING_REWRITE_SHA, MALFORMED_LEASE),
        )

        recorded = transfers._pending_rewrite(self._durable())

        self.assertFalse(recorded.is_recorded)
        self.assertTrue(recorded.damaged)

    def test_another_commit_resets_the_replay(self) -> None:
        # The record is whole and vouches for some other head, which is the
        # attempt's own answer that this checkout is not its work. Only a
        # comment carrying none of the group reaches the counts.
        self._assert_resets_the_replay(FOREIGN_SHA)

    def _assert_resets_the_replay(self, recorded) -> None:
        """One hand-edited head, and the reset-and-park it has to earn."""
        self._crashes_before_the_grant()
        self._edited(
            lambda state: state.set(KEY_PENDING_REWRITE_SHA, recorded),
        )

        self._assert_reset_and_parked(self._resumes())

    def _assert_reset_and_parked(self, resumed) -> None:
        """Nothing published, the branch back on the anchor, a human asked."""
        self._assert_nothing_left(resumed)
        self._assert_reset_once(resumed)
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])
        self._assert_nothing_readjudicated()
        self._assert_anchor(None)
        self._assert_parked(PARK_FAILED)


class RelabelledFinishTest(_ResumedRebaseCase, unittest.TestCase):
    """The route this recovery's own finish had already most of the way made."""

    def test_only_the_last_write_is_made(self) -> None:
        # The premise: the reviewer has been routed at the rewritten head, the
        # finish has recorded that it said so, and the write that clears the
        # attempt never happened. The relabel is this route's own last step
        # before that write -- read as somebody else's move, the tick with
        # only the write left to make would park for a human forever. And the
        # notice, the audit event, and the route all went out before the write
        # the crash swallowed, so the tick that makes it owes none of them: a
        # second `base_rebased` would be filed under the stage the relabel
        # moved to, for one publication that happened once.
        self._crashes_after_the_relabel()
        crashed = dict(self._pinned())

        resumed = self._resumes(remote_head=AFTER_SHA)

        self.assertEqual(crashed[KEY_ANNOUNCED_SHA], AFTER_SHA)
        self.assertEqual(crashed[KEY_PENDING_PUSH_SHA], BEFORE_SHA)
        self._assert_nothing_left(resumed)
        self._assert_finished_the_route(CLEAN_REBASE)
        self._assert_said_once()
        self._assert_settled_once()

    def _assert_said_once(self) -> None:
        """The finish that crashed announced itself, and this one did not."""
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])
        rebased = self._events_of(EVENT_BASE_REBASED)
        self.assertEqual(
            [record[METHOD_FIELD] for record in rebased], [CLEAN_REBASE],
        )
        self.assertEqual(rebased[0][STAGE_FIELD], LABEL)
        self.assertEqual(len(list(self.gh.posted_pr_comments)), 1)


class UnpairedPermissionTest(_ResumedRebaseCase, unittest.TestCase):
    """A permission whose debt was written with it and is not there now."""

    def test_a_damaged_identity_resets_the_replay(self) -> None:
        # The permission still reads back whole, and what it is a claim about
        # -- the verdict and the contribution under it -- no longer does.
        # Believed, the settlement re-asks a permit whose accepted pair cannot
        # be fingerprinted, the ordinary gate measures the replay instead, and
        # a change a human already ruled on is published and announced.
        self._crashes_before_the_push()
        self._edited(lambda state: state.data.pop(DAMAGED_IDENTITY_FIELD))

        resumed = self._resumes()

        self._assert_nothing_left(resumed)
        self._assert_reset_once(resumed)
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])
        self.assertEqual(self._events_of(EVENT_TRANSFER), [])
        self._assert_nothing_readjudicated()
        self._assert_routed(False)
        self._assert_parked(PARK_FAILED)

    def test_an_unpaired_permission_resets_the_replay(self) -> None:
        # The grant writes the permission and the debt in one statement for
        # one commit. Read as outstanding, the settlement re-asks the permit
        # -- and a permit that grants re-writes BOTH, so the missing half
        # would be reconstructed from the very claim nobody could check and
        # the push would go out under it.
        self._crashes_before_the_push()
        self._edited(lambda state: state.set(KEY_APPROVED_SHA, None))

        resumed = self._resumes()

        self._assert_nothing_left(resumed)
        self._assert_reset_once(resumed)
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])
        self.assertEqual(self._events_of(EVENT_TRANSFER), [])
        self._assert_nothing_readjudicated()
        self._assert_parked(PARK_FAILED)

    def test_a_repointed_debt_resets_the_replay(self) -> None:
        # The same disagreement one field over: a debt owed for a commit this
        # permission was never granted for. Its lease still names the anchor,
        # so the refresh is not frozen out and the recovery has to answer.
        self._crashes_before_the_push()
        self._edited(lambda state: state.set(KEY_APPROVED_SHA, FOREIGN_SHA))

        resumed = self._resumes()

        self._assert_nothing_left(resumed)
        self._assert_reset_once(resumed)
        self.assertEqual(self._events_of(EVENT_TRANSFER), [])
        self._assert_parked(PARK_FAILED)


class DisabledSwitchRecoveryTest(_ResumedRebaseCase, unittest.TestCase):
    """A recovery that comes back to find decomposition switched off.

    The switch is what decides whether an ordinary tick measures at all, and
    off it takes every candidate straight past the gate unread. That is the
    right answer for work nobody has ruled on and the wrong one here: what a
    recovery needs from the gate is not a measurement but the PERMIT, which is
    the only thing that can say a replay carries a verdict a human already
    gave. Taken past it, the transfer never settles, the exemption stays on a
    commit this branch no longer has, and the push lands with the record of
    why it was allowed to still owed.
    """

    def test_an_unrecorded_rewrite_still_settles(self) -> None:
        # The window before the grant, where the recovery has to assemble the
        # evidence for itself. Nothing about that work is a measurement, and
        # the switch has nothing to say about it.
        self._crashes_before_the_grant()

        resumed = self._resumes_switched_off()

        self._assert_settled_by(resumed)

    def test_an_outstanding_permission_still_settles(self) -> None:
        # And the window after it, where the record already says what the
        # push may carry: the permit is re-asked over a group the switch was
        # on for, and the switch off would drop it on the floor.
        self._crashes_before_the_push()

        resumed = self._resumes_switched_off()

        self._assert_settled_by(resumed)

    def _resumes_switched_off(self):
        """The next tick, with the decomposition kill switch flipped off."""
        with patch.object(config, CONFIG_DECOMPOSE, False):
            return self._resumes()

    def _assert_settled_by(self, resumed) -> None:
        """The verdict moved onto the replay the reissued push published."""
        pushed = resumed[PUSH_PATCH].call_args.kwargs
        self.assertEqual(pushed[REVISION], AFTER_SHA)
        self.assertEqual(pushed[LEASE], BEFORE_SHA)
        self._assert_settled_once()
        self._assert_nothing_readjudicated()
        self._assert_finished_the_route(RECOVERY_PUSHED)


class AdvancedBaseFinishTest(_ResumedRebaseCase, unittest.TestCase):
    """A finish to resume, over a base that moved again in the meantime."""

    def setUp(self) -> None:
        super().setUp()
        self._crashes_at_the_relabel()
        self.resumed = self._resumes(remote_head=AFTER_SHA, behind=True)

    def test_the_tick_falls_through_to_the_rebase(self) -> None:
        # The lag outlives the recovery: whatever the announcement says was
        # published, this branch is still behind the base and still owes the
        # rebase that brings it forward. Claimed by the finish, that rebase
        # never runs -- and nothing on the next tick brings it back, because
        # the anchor that would have is gone.
        self.resumed[REBASE_PATCH].assert_called_once()

    def test_the_stale_head_is_not_routed_to(self) -> None:
        # The head the announcement was made at is behind the base already.
        # Relabelling there sends the reviewer to a commit this same tick is
        # replacing, and spends the round on it.
        self._assert_routed(False)
        self.assertEqual(
            [record[METHOD_FIELD] for record in self._events_of(
                EVENT_BASE_REBASED,
            )],
            [CLEAN_REBASE],
        )

    def test_the_finish_is_made_durable_first(self) -> None:
        # Falling through is not forgetting: the record of the announced
        # route goes down before the rebase runs, so a crash between them
        # comes back to an issue with nothing half-said on it.
        self._assert_anchor(None)
        self.assertIsNone(self._pinned()[KEY_ANNOUNCED_SHA])
        self.assertEqual(self._pinned()[KEY_REVIEW_ROUND], 0)

    def test_the_verdict_is_still_moved_once(self) -> None:
        self._assert_settled_once()
        self._assert_nothing_readjudicated()


class AnnouncedForeignMoveTest(_ResumedRebaseCase, unittest.TestCase):
    """The route's own mark, beside a label the route never writes.

    An announcement is what tells this recovery that the relabel it finds is
    its own last step rather than somebody else's move -- but that reading is
    only available for the ONE label a finish moves to. Any other stage is a
    hand at the issue, and a mark left by the interrupted tick says nothing
    about it.
    """

    def setUp(self) -> None:
        super().setUp()
        self._crashes_at_the_relabel()
        self.gh.set_workflow_label(self._issue(), WorkflowLabel.FIXING)
        self.resumed = self._resumes(remote_head=AFTER_SHA)

    def test_the_moved_issue_is_not_finished(self) -> None:
        # Forgiven, the finish would route an issue a human moved to a stage
        # it was never published under, and drop the anchor on the way.
        self._assert_routed(False)
        self._assert_anchor(BEFORE_SHA)
        self._assert_parked(PARK_FAILED)

    def test_nothing_is_pushed_or_reset_over_it(self) -> None:
        self._assert_nothing_left(self.resumed)
        self.assertEqual(self._resets_of(self.resumed), [])
        self._assert_nothing_readjudicated()

    def test_the_record_it_is_held_by_is_kept(self) -> None:
        # The park is only useful while the attempt is still readable: an
        # operator putting the label back is what lets the ordinary recovery
        # finish this route on its own terms.
        pinned = self._pinned()
        self.assertEqual(pinned[KEY_PENDING_REWRITE_SHA], AFTER_SHA)
        self.assertEqual(pinned[KEY_ANNOUNCED_SHA], AFTER_SHA)


class DamagedCheckpointTest(_ResumedRebaseCase, unittest.TestCase):
    """The two notes a finish leaves about itself, read by presence.

    Neither is evidence for a decision; both are marks saying how far the tick
    that made them got, and both are dropped by the write that ends the
    attempt. So a mark standing at all says its window is still open, and one
    standing over a value this build cannot square with the head in hand is a
    comment something took apart. Read as "nothing owed" they cost the two
    things a recovery exists to protect: the record of a verdict that moved,
    and the pull request's account of what published it.
    """

    def test_a_malformed_proof_holds_the_route(self) -> None:
        # The proof is kept until the `late_transfer` record is out. Read as
        # absent, the route finishes -- the anchor that brings this tick back
        # is dropped, no record reaches either sink, and the damaged proof is
        # left standing for nobody.
        self._crashes_before_the_route()
        self._edited(lambda state: state.set(KEY_REWRITE_PROOF, DAMAGED_PROOF))

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        self._assert_anchor(BEFORE_SHA)
        self._assert_parked(PARK_PUSH_FAILED)
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self._assert_routed(False)

    def test_a_foreign_mark_holds_the_route(self) -> None:
        # The mark says a finish already announced THIS attempt's replay, so
        # one naming any other head cannot say whether the notice and the
        # audit event are out. Read as "not announced", the tick says both
        # again for a publication that happened once.
        self._crashes_at_the_relabel()
        self._edited(lambda state: state.set(KEY_ANNOUNCED_SHA, FOREIGN_SHA))

        resumed = self._resumes(remote_head=AFTER_SHA)

        self._assert_nothing_left(resumed)
        self._assert_anchor(BEFORE_SHA)
        self._assert_parked(PARK_PUSH_FAILED)
        self.assertEqual(
            [record[METHOD_FIELD] for record in self._events_of(
                EVENT_BASE_REBASED,
            )],
            [CLEAN_REBASE],
        )


class TerminalPullRequestTest(_ResumedRebaseCase, unittest.TestCase):
    """An attempt still in flight for a pull request that has been merged.

    The anchor is the flag the refresh comes back for and the least of what
    the attempt left. The debt the gate recorded before the push says one
    commit is still owed a publication onto this pull request, and the
    permission beside it says what that push may carry a human's verdict
    over. A merged pull request can receive neither -- and left standing, the
    debt is what the reconciliation ahead of the next handler tries to pay,
    parking the issue on a publication it cannot even enter while the stage
    that would finalize the merge never runs.
    """

    def test_a_merged_pull_request_retires_the_debt(self) -> None:
        # The authorization boundary: the permit granted, the debt written,
        # and the push never made -- then the pull request merged without it.
        self._crashes_before_the_push()
        self._merges_the_pull_request()

        self._resumes()

        pinned = self._pinned()
        self.assertIsNone(pinned[KEY_PENDING_PUSH_SHA])
        self.assertIsNone(pinned[KEY_APPROVED_SHA])
        durable = self._durable()
        self.assertFalse(_rewrites.carries_rewrite_authorization(durable))
        # The grant moved nothing, so the verdict is still on the commit the
        # merge carried.
        self.assertTrue(_exemption.is_exempt(durable, BEFORE_SHA))
        self._assert_finalizable()

    def test_a_merged_request_keeps_a_settled_move(self) -> None:
        # The receipting boundary one step on: the push landed, the receipt
        # carried the verdict over, and the route never finished. There is no
        # debt left to retire and the transfer is over, so what the retirement
        # may not do is undo either.
        self._crashes_before_the_route()
        self._merges_the_pull_request()

        self._resumes(remote_head=AFTER_SHA)

        self.assertIsNone(self._pinned()[KEY_PENDING_PUSH_SHA])
        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, AFTER_SHA))
        self.assertEqual(
            _rewrites.read_rewrite_authorization(durable).phase,
            _rewrites.LateRewritePhase.PUBLISHED,
        )
        self._assert_finalizable()

    def _merges_the_pull_request(self) -> None:
        """Take the pull request this attempt was made for out of `open`."""
        pull_request = self.gh.pulls[PR_NUMBER]
        pull_request.merged = True
        pull_request.state = PR_STATE_CLOSED

    def _assert_finalizable(self) -> None:
        """The reconciliation lets the stage that finalizes a merge run."""
        issue = self._issue()
        held = _reconcile._reconciles_published_work(
            self.gh, self.spec, issue, WorkflowLabel(LABEL),
            self.gh.read_pinned_state(issue),
        )

        self.assertFalse(held)
        self.assertNotIn(KEY_PARK_REASON, self._pinned())


class DeferredRecoveryTest(_ResumedRebaseCase, unittest.TestCase):
    """A recovery the refresh could not take, and who owns the tick behind it.

    The refresh reads the pull request before it will run a recovery -- a
    terminal one belongs to the stage that finalizes it -- so a `get_pr` that
    fails leaves the attempt exactly where the crash left it: the anchor
    pinned, the replay on the branch, and the debt the gate recorded before
    the push still standing. That debt is the same shape the reconciliation
    ahead of every handler pays, and paying it there is the wrong owner
    finishing the wrong half: the commit publishes and settles while the
    anchor, the reviewer's round, and the route the recovery owes are left
    exactly as the dead tick left them.
    """

    def test_an_unread_pull_request_holds_the_tick(self) -> None:
        self._crashes_before_the_push()
        with self._unreadable():
            resumed = self._resumes()

        pushed, held = self._reconciles()

        # The refresh got no further than the read, so the attempt is whole.
        self._assert_nothing_left(resumed)
        self._assert_anchor(BEFORE_SHA)
        self.assertEqual(self._pinned()[KEY_APPROVED_SHA], AFTER_SHA)
        # And the tick stops rather than publishing it under another owner:
        # the push that would settle the debt is never made, the round the
        # recovery's own finish resets is left where the reviewer spent it,
        # and nothing is parked for a read that will answer next tick.
        self.assertTrue(held)
        pushed.assert_not_called()
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self.assertEqual(self._pinned()[KEY_REVIEW_ROUND], SPENT_ROUNDS)
        self.assertNotIn(KEY_PARK_REASON, self._pinned())

    def test_a_no_debt_seam_holds_the_tick_too(self) -> None:
        # The two windows that leave the anchor and nothing this owner reads:
        # a tick that died before the transfer was granted, so nothing was
        # measured and no debt was recorded, and one that died past the
        # receipt, whose settlement cleared the debt it had. Answered as
        # "this issue owes the reconciliation nothing", both let the stage
        # run behind a recovery no tick has finished.
        for described, crashes in _NO_DEBT_SEAMS.items():
            with self.subTest(seam=described):
                self.setUp()
                getattr(self, crashes)()
                with self._unreadable():
                    self._resumes()

                pushed, held = self._reconciles()

                self.assertTrue(held)
                pushed.assert_not_called()
                self._assert_anchor(BEFORE_SHA)
                pinned = self._pinned()
                self.assertIsNone(pinned.get(KEY_APPROVED_SHA))
                self.assertEqual(pinned[KEY_REVIEW_ROUND], SPENT_ROUNDS)
                self.assertNotIn(KEY_PARK_REASON, pinned)

    def _unreadable(self):
        """A pull-request read no request on this tick can take."""
        return patch.object(
            self.gh, GET_PR,
            MagicMock(side_effect=RuntimeError(UNREADABLE_PR)),
        )

    def _reconciles(self):
        """Run the reconciliation every handler is dispatched behind.

        The push seam answers as a remote that would TAKE it, so what stops
        the publication is this owner standing down rather than a request
        that failed.
        """
        issue = self._issue()
        pushed = MagicMock(return_value=True)
        with patch.object(_branch_transport, PUSH_BRANCH, pushed):
            held = _reconcile._reconciles_published_work(
                self.gh, self.spec, issue, WorkflowLabel(LABEL),
                self.gh.read_pinned_state(issue),
            )
        return pushed, held


class StrandedAttemptTest(_ResumedRebaseCase, unittest.TestCase):
    """An attempt whose issue was moved off the refresh-driven set entirely.

    The clear this label reaches is for an anchor over a checkout still
    standing on it, which is only a promise to come back. An attempt that got
    as far as a recorded rewrite and a granted permission is a different
    thing: cleared, the replay stops being attributable to anything, the
    verdict is licensed onto a commit no push carried, and the issue it hands
    on is one no reader can tell from an issue with nothing in flight. So is
    one that got as far as `git rebase` and no further, since the terms it
    left cannot tell that window from an attempt that never started.
    """

    def test_a_granted_attempt_is_parked_whole(self) -> None:
        # Everything the clear would have come apart, in one resumption: the
        # anchor and the replay it names, the permission and the debt a later
        # grant would trip over, and the exemption still on the commit a
        # human ruled on. Nothing is pushed, reset, or rebased for it either
        # -- no road runs under a label the refresh does not drive, and the
        # hand that moved it may have moved the checkout too.
        self._crashes_before_the_push()
        self._moves_the_issue_off()

        resumed = self._resumes()

        self._assert_anchor(BEFORE_SHA)
        self.assertEqual(self._pinned()[KEY_PENDING_REWRITE_SHA], AFTER_SHA)
        self._assert_permission_standing()
        self._assert_nothing_ran(resumed)
        self._assert_parked(PARK_FAILED)

    def test_an_unwritten_replay_is_parked_too(self) -> None:
        # The crash seam no id can close, found under this same label: `git
        # rebase` has replayed the branch and the write naming what it
        # produced never happened, so the comment carries the anchor and the
        # terms alone. Cleared there, the rewrite is left on the branch with
        # nothing naming it and the stage this label names is free to start
        # over on a change a human already ruled on.
        self._crashes_before_the_record()
        self._moves_the_issue_off()

        self._resumes()

        self._assert_anchor(BEFORE_SHA)
        pinned = self._pinned()
        self.assertNotIn(KEY_PENDING_REWRITE_SHA, pinned)
        self.assertEqual(pinned[KEY_PENDING_REWRITE_PR], PR_NUMBER)
        self._assert_parked(PARK_FAILED)

    def test_a_repeated_poll_says_it_once(self) -> None:
        # The park keeps the record, and the record is what brings this route
        # back -- so every poll under the wrong label arrives here again over
        # a comment nothing has changed. Said again each time, the thread
        # fills with one sentence repeated and each park ratchets the
        # watermark past whatever the operator wrote, hiding the reply that
        # would release the attempt behind the orchestrator's own comment.
        self._crashes_before_the_push()
        self._moves_the_issue_off()
        self._resumes()
        parked = dict(self._pinned())
        said = len(self._issue_comments())

        self._resumes()

        self.assertEqual(len(self._issue_comments()), said)
        self.assertEqual(self._pinned(), parked)
        self._assert_anchor(BEFORE_SHA)
        self._assert_permission_standing()

    def test_a_bare_anchor_is_still_cleared(self) -> None:
        # What says the parks above are about what the attempt left rather
        # than about the label: the same relabel over an anchor whose
        # checkout never moved drops it and says nothing to anybody. The
        # label is the conflict stage rather than the adjudication because
        # the checkout here is back on the exempt commit, and an issue under
        # `workflow:decomposing` standing on the commit it is adjudicating is
        # held out of the refresh by the freeze ahead of this route.
        self._crashes_before_the_push()
        self._moves_the_issue_off(WorkflowLabel.RESOLVING_CONFLICT)
        self._forgets_the_attempt()

        self._resumes(local_head=BEFORE_SHA)

        self._assert_anchor(None)
        self.assertNotIn(KEY_PARK_REASON, self._pinned())

    def _issue_comments(self) -> list[str]:
        """Every comment this route has put on the issue thread."""
        return [
            body for number, body in self.gh.posted_comments
            if number == ISSUE
        ]

    def _assert_nothing_ran(self, resumed) -> None:
        """No push, no reset, no rebase, and no reading of the replay.

        The last of those is what stops the stage this label names from
        putting a second agent on a change a human already ruled on.
        """
        self._assert_nothing_left(resumed)
        self.assertEqual(self._resets_of(resumed), [])
        self.assertEqual(self._events_of(EVENT_BASE_REBASED), [])
        self.assertEqual(self._events_of(EVENT_MEASUREMENT), [])

    def _assert_permission_standing(self) -> None:
        """The grant and the verdict it was made over are where they were."""
        durable = self._durable()
        self.assertTrue(_rewrites.carries_rewrite_authorization(durable))
        self.assertEqual(self._pinned()[KEY_REWRITE_PHASE], PHASE_AUTHORIZED)
        self.assertTrue(_exemption.is_exempt(durable, BEFORE_SHA))

    def _moves_the_issue_off(
        self, label: WorkflowLabel = WorkflowLabel.DECOMPOSING,
    ) -> None:
        """Take the issue to a label the base refresh does not drive."""
        self.gh.set_workflow_label(self._issue(), label)

    def _forgets_the_attempt(self) -> None:
        """Take the crashed tick's own records off the pinned comment."""
        self._edited(_drops_the_attempt_records)


def _drops_the_attempt_records(state) -> None:
    """Leave one anchor standing and nothing beside it."""
    _rewrites.clear_rewrite_authorization(state)
    for key in ATTEMPT_RECORD_KEYS:
        state.set(key, None)


class InFlightRewriteRecoveryTest(_ResumedRebaseCase, unittest.TestCase):
    """The rebase that reached the branch before any record reached disk.

    The one window no id can close. `git rebase` has replayed the branch, the
    write naming what it produced never happened, and the checkout diverges
    from the head the pull request still carries -- which is the same shape a
    worktree somebody rebuilt leaves. Nothing on the comment names that
    commit, so what it is decided on instead is what it CONTRIBUTES: the
    permit re-fingerprints it against the pair a human ruled on, and the terms
    the anchor carries say which publication to ask that over.
    """

    def setUp(self) -> None:
        super().setUp()
        self._crashes_before_the_record()
        self.crashed = dict(self._pinned())
        self.resumed = self._resumes(diverged=REBASED_COUNTS)

    def test_the_crash_left_the_terms_and_no_head(self) -> None:
        # The premise: the attempt is on the comment as one still in flight --
        # the anchor it pinned and the publication it was entered under -- and
        # the replay it made is named nowhere.
        self.assertEqual(self.crashed[KEY_PENDING_PUSH_SHA], BEFORE_SHA)
        self.assertEqual(self.crashed[KEY_PENDING_REWRITE_PR], PR_NUMBER)
        self.assertEqual(self.crashed[KEY_PENDING_REWRITE_STAGE], LABEL)
        self.assertNotIn(KEY_PENDING_REWRITE_SHA, self.crashed)

    def test_the_replay_is_published_on_the_permit(self) -> None:
        # Read by the divergence counts -- the only thing left when no record
        # names the head -- this is a branch that parks: a real rebase is
        # ahead of the anchor and behind it at once.
        pushed = self.resumed[PUSH_PATCH].call_args.kwargs
        self.assertEqual(pushed[REVISION], AFTER_SHA)
        self.assertEqual(pushed[LEASE], BEFORE_SHA)
        self._assert_settled_once()

    def test_nothing_is_measured_or_rebased_again(self) -> None:
        self._assert_nothing_readjudicated()
        self._assert_finished_the_route(RECOVERY_PUSHED)


class UnprovenInFlightTest(_ResumedRebaseCase, unittest.TestCase):
    """What the in-flight window costs where nothing can prove the head.

    The road is opened by the evidence and by nothing else. Every state that
    cannot produce it resets and parks, because the only thing behind this
    road is the cumulative reading -- and a count says how big a change is,
    never whose it is.
    """

    def test_a_changed_contribution_refuses(self) -> None:
        # The permit is asked and says no: what the checkout adds to the base
        # is not what the adjudication accepted, so this is not a replay of
        # that change however the branch got here.
        self._crashes_before_the_record()
        self.reading.digests.pop((REPLAYED_BASE_SHA, AFTER_SHA))

        resumed = self._resumes(diverged=REBASED_COUNTS)

        self._assert_reset_and_parked(resumed)

    def test_an_unprovable_verdict_parks(self) -> None:
        # A legacy exemption names the commit and nothing says what it
        # contributes, so there is no pair to re-fingerprint the checkout
        # against. Fallen through to the counts, a divergent head nothing
        # vouches for would be measured and force-pushed.
        self._legacy_verdict()
        self._crashes_before_the_record()

        resumed = self._resumes(diverged=REBASED_COUNTS)

        self._assert_reset_and_parked(resumed)

    def test_an_issue_with_no_verdict_still_parks(self) -> None:
        # Nothing to prove anything against at all: the road is not opened,
        # and the divergent checkout resets onto the anchor and parks.
        self._forgets_the_verdict()
        self._crashes_before_the_record()

        resumed = self._resumes(diverged=REBASED_COUNTS)

        self._assert_nothing_left(resumed)
        self._assert_reset_once(resumed)
        self._assert_anchor(None)
        self._assert_parked(PARK_PUSH_FAILED)

    def test_a_relabel_in_the_window_parks(self) -> None:
        # The terms go down before git runs precisely so this is visible: the
        # permit's publication checks are asked against the stage the attempt
        # was entered from, and an issue moved while the process was down is
        # one the attempt was never made for.
        self._crashes_before_the_record()
        self.gh.set_workflow_label(self._issue(), WorkflowLabel.DOCUMENTING)

        resumed = self._resumes(diverged=REBASED_COUNTS)

        self._assert_nothing_left(resumed)
        self.assertEqual(self._resets_of(resumed), [])
        self._assert_anchor(BEFORE_SHA)
        self._assert_parked(PARK_FAILED)

    def _assert_reset_and_parked(self, resumed) -> None:
        """Nothing published, the branch back on the anchor, a human asked."""
        self._assert_nothing_left(resumed)
        self._assert_reset_once(resumed)
        self._assert_nothing_readjudicated()
        self._assert_anchor(None)
        self._assert_parked(PARK_FAILED)
        self.assertTrue(_exemption.is_exempt(self._durable(), BEFORE_SHA))

    def _legacy_verdict(self) -> None:
        """Re-record this issue's verdict in the shape that names no pair."""
        self._edited(_drops_the_semantic_identity)

    def _forgets_the_verdict(self) -> None:
        """Take the adjudication off the issue entirely."""
        self._edited(_drops_the_exemption)


def _drops_the_semantic_identity(state) -> None:
    """Leave the exempt commit named and nothing saying what it contributes."""
    for key in IDENTITY_KEYS:
        state.data.pop(key, None)


def _drops_the_exemption(state) -> None:
    """Leave an issue no adjudication ever ruled on."""
    _exemption.clear_exemption(state)
