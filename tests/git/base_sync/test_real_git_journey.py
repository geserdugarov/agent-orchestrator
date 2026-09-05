# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""One oversized change, adjudicated once and reviewed twice, on a real repo.

The whole road an accepted candidate takes when the base moves under it, with
nothing about it seeded. A `workflow:validating` round publishes a change the
real counter reads past the ceiling, so the real size gate holds it and hands
the issue to the adjudication; the real adjudicator answers `single`, and the
settlement records the exemption and the digest of what that commit
contributes over the pair it froze. Then the base advances on the real remote
and the per-tick refresh replays the branch onto it and force-publishes the
result.

That replay is a commit no human ever saw, and everything here turns on it
being recognized as the change they already ruled on. The exemption and the
receipt move onto it, the push that put it there is leased to the head the
pull request was standing on, and the tick after it is the real
`workflow:validating` handler: the reviewer goes round again over the
rewritten checkout, approves, and the issue leaves for the documentation pass
-- with one measurement, one verdict, one trip through `workflow:decomposing`,
and two adjudication comments for the life of the issue, all of them naming
the commit a human was actually asked about.

The crashes are the second half of the same journey, and nothing about them
is seeded either. Each one runs this same road up to one of the five moments
an auto rebase makes durable -- the anchor, the replay, the permission, the
push, the receipt -- lets the process die there, and runs the next real
refresh over whatever that left on disk and on the comment. The window
BETWEEN the first two is one of them: `git rebase` has moved the branch and
nothing has written which commit that produced, which is the one state no id
can answer for. Every one of them has to come back to the same finish.

Only three things are stood in for, and none of them is a decision: the two
agents' replies, the authenticated push, and the remote-side base freeze this
fixture has no token to take.
"""
from __future__ import annotations

import unittest

from orchestrator.workflow.late_split import (
    exemption as _exemption,
    rewrites as _rewrites,
)
from tests.git.base_sync.exemption_git_support import ISSUE, events_of
from tests.git.base_sync.journey_git_support import (
    OversizedJourneyRealGitFixture,
    _LandsThenDies,
)
from tests.git.base_sync.real_git_test_support import PR_NUMBER
from tests.workflow.fixtures import (
    LABEL_DECOMPOSING,
    LABEL_DOCUMENTING,
    LABEL_VALIDATING,
)

EVENT_MEASUREMENT = "late_measurement"
EVENT_TRANSFER = "late_transfer"
EVENT_REBASED = "base_rebased"

METHOD_FIELD = "method"
CLEAN_REBASE = "auto_clean_rebase"
RECOVERY_PUSHED = "crash_recovery_pushed"
RECOVERY_RELABELLED = "crash_recovery_relabel_only"

# The record one interrupted attempt leaves, which every finish drops.
KEY_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"
KEY_PENDING_REWRITE_SHA = "pending_auto_base_rebase_rewrite_sha"
EVENT_VERDICT = "late_verdict"
EVENT_REVIEW = "review_verdict"

VERDICT_FIELD = "verdict"
APPROVED = "approved"

# The two comments one adjudication puts on the issue thread: the notice that
# a push would take the pull request past the ceiling, and the verdict a human
# reached about it.
ADJUDICATION_COMMENTS = 2

KEY_PUBLISHED_SHA = "implementing_published_sha"
KEY_REVIEW_ROUND = "review_round"

# The flag every fail-closed park in this domain sets, and the one a rotation
# that resumed cleanly must not have.
KEY_AWAITING_HUMAN = "awaiting_human"


class AdjudicatedRebaseJourneyTest(
    OversizedJourneyRealGitFixture, unittest.TestCase,
):
    """The change a human ruled on, carried through the base advance under it."""

    def setUp(self) -> None:
        super().setUp()
        self.accepted = self._commits_an_oversized_candidate()
        self.held = self._publishes_the_candidate(self.accepted)
        self.settled = self._accepted_as_single()
        self._advance_base(conflicting=False)
        self.pushed = self._refreshes()
        self.replayed = self._wt_head()

    def test_the_replay_carries_the_verdict_over(self) -> None:
        # The whole of what the refresh leg leaves, in one reading of it. The
        # premise is real rather than seeded: the gate counted the diff past
        # the ceiling, held the publication, and the adjudicator's `single`
        # put the verdict on the comment. Then the replay -- named against
        # the commit the gate proved and leased to the head the pull request
        # was standing on, so a branch somebody else moved rejects it instead
        # of being overwritten -- and the exemption, its identity, and the
        # receipt all on the far side of it, since a reader may never see a
        # verdict for a commit no remote carries. Last the route: a new head
        # to vote on, so the round the reviewer had spent is reset and the
        # issue goes back to them rather than on to a merge gate.
        self._assert_adjudicated_once()
        self.assertNotEqual(self.replayed, self.accepted)
        self.assertEqual(self.pushed.revision, self.replayed)
        self.assertEqual(self.pushed.force_with_lease, self.accepted)
        durable = self._durable()
        self._assert_rotated_onto(durable, self.replayed)
        self.assertEqual(
            _exemption.read_semantic_identity(durable).base_sha,
            self._merge_base(),
        )
        self.assertEqual(durable.get(KEY_PUBLISHED_SHA), self.replayed)
        self.assertIn((ISSUE, LABEL_VALIDATING), self._gh.label_history)
        self.assertEqual(durable.get(KEY_REVIEW_ROUND), 0)

    def test_the_rerun_adjudicates_nothing_again(self) -> None:
        # The real `workflow:validating` tick over the checkout the refresh
        # rewrote, and everything it settles. One reviewer round, one verdict,
        # and the approval carrying the issue on to the documentation pass --
        # with the journey still counting one reading, one verdict, one trip
        # through the adjudication, and the two comments that trip posted,
        # all of them naming the commit a human was actually asked about. Two
        # rewrites now separate that commit from the head the approval leaves
        # -- the replay and the squash -- and the exemption is past both, so a
        # later reading of that head finds the change already decided.
        reviewer = self._reviews()

        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(
            [record[VERDICT_FIELD] for record in events_of(self, EVENT_REVIEW)],
            [APPROVED],
        )
        self.assertIn((ISSUE, LABEL_DOCUMENTING), self._gh.label_history)
        self._assert_adjudicated_once()
        announced = self._issue_comments()
        self.assertEqual(len(announced), ADJUDICATION_COMMENTS)
        for body in announced:
            self.assertIn(self.accepted, body)
        approved = self._wt_head()
        self.assertNotEqual(approved, self.accepted)
        self.assertTrue(_exemption.is_exempt(self._durable(), approved))

    def _assert_adjudicated_once(self) -> None:
        """One reading, one verdict, and one trip through the adjudication."""
        self.assertTrue(self.held.held)
        self.assertEqual(len(events_of(self, EVENT_MEASUREMENT)), 1)
        self.assertEqual(len(events_of(self, EVENT_VERDICT)), 1)
        self.assertEqual(
            self._gh.label_history.count((ISSUE, LABEL_DECOMPOSING)), 1,
        )

    def _assert_rotated_onto(self, durable, published: str) -> None:
        """The verdict is on this commit and its permission is spent."""
        self.assertTrue(_exemption.is_exempt(durable, published))
        self.assertEqual(
            _rewrites.read_rewrite_authorization(durable).phase,
            _rewrites.LateRewritePhase.PUBLISHED,
        )


if __name__ == "__main__":
    unittest.main()


class CrashedJourneyRealGitTest(
    OversizedJourneyRealGitFixture, unittest.TestCase,
):
    """The same journey, stopped at each moment it makes durable.

    One auto rebase of the adjudicated commit writes five things in order --
    the anchor, the replay it produced, the permission, the push, the receipt
    -- and announces itself last. A process can be lost in any window between
    them, and what every one of them has to come back to is the same finish:
    the verdict on the replay, the reviewer routed to it, and nobody asked to
    adjudicate the change a second time.

    Nothing about the crash is seeded. Each case runs the real refresh up to
    the moment it is about, lets the process die there, and runs the next
    real refresh over whatever that left on disk and on the comment.
    """

    def setUp(self) -> None:
        super().setUp()
        self.accepted = self._commits_an_oversized_candidate()
        self._publishes_the_candidate(self.accepted)
        self._accepted_as_single()
        self._advance_base(conflicting=False)

    def test_a_crash_before_the_record_is_recovered(self) -> None:
        # `git rebase` has replayed the branch and the write naming what it
        # produced never happened, so the checkout diverges from the head the
        # pull request carries and no id on the comment names it. Read by the
        # divergence alone that is a branch this recovery resets -- and the
        # replay of a change a human already ruled on is thrown away.
        self._crashes(self._dies_before_the_record())
        self._assert_left_a_replay_nothing_names()

        pusher = self._refreshes()

        self._assert_finished(pusher, RECOVERY_PUSHED)

    def test_a_crash_after_the_rewrite_is_recovered(self) -> None:
        # The narrowest window: git has replayed the branch and the attempt
        # has recorded what it produced, and nothing else has run. Without
        # that record the replay is only a divergence, and the recovery would
        # reset a perfectly good rewrite off the branch.
        self._crashes(self._dies_after_the_rewrite())

        pusher = self._refreshes()

        self._assert_finished(pusher, RECOVERY_PUSHED)

    def test_a_crash_before_the_grant_is_recovered(self) -> None:
        # The replay is on the branch and nothing on the comment names it as
        # a rewrite yet, so the recovery re-derives the evidence the dead tick
        # would have assembled and the permit rules on that.
        self._crashes(self._dies_before_the_grant())

        pusher = self._refreshes()

        self._assert_finished(pusher, RECOVERY_PUSHED)

    def test_a_crash_before_the_push_is_recovered(self) -> None:
        # The permission is durable and the remote is still on the head the
        # push was leased against, so the recovery reissues it.
        self._crashes(self._dies_before_the_push())

        pusher = self._refreshes()

        self._assert_finished(pusher, RECOVERY_PUSHED)

    def test_a_crash_before_the_receipt_is_recovered(self) -> None:
        # The push landed and the write that receipts it did not, so the pull
        # request carries a replay the comment still says is owed one. The
        # leased no-op is what proves it and carries the receipt.
        self._crashes(self._dies_before_the_push_returns())

        pusher = self._refreshes()

        self._assert_finished(pusher, RECOVERY_RELABELLED)

    def test_a_crash_before_the_route_is_recovered(self) -> None:
        # Everything durable landed and the notice, the event, and the route
        # did not, so the recovery has only the route left to make.
        self._crashes(self._dies_before_the_route())

        pusher = self._refreshes()

        self._assert_finished(pusher, RECOVERY_RELABELLED)

    def test_a_crash_before_the_report_is_recovered(self) -> None:
        # The receipt landed and the record it owes the sinks did not, over a
        # proof no later reading could re-derive. The settlement keeps that
        # proof until the record is out, so the recovery makes it.
        self._crashes(self._dies_before_the_report())

        self._refreshes()

        self._assert_settled()
        self._assert_reviewable()

    def test_a_crash_before_the_mark_repeats_once(self) -> None:
        # The one window the checkpoint cannot close: the notice and the event
        # are out and nothing records that they are. It resolves toward
        # saying them again rather than losing them, since a record a reader
        # can see twice beats one nobody can reconstruct -- and the durable
        # state is right either way.
        self._crashes(self._dies_before_the_checkpoint())

        self._refreshes()

        self._assert_settled()
        self._assert_reviewable()
        self.assertEqual(
            [record[METHOD_FIELD] for record in events_of(self, EVENT_REBASED)],
            [CLEAN_REBASE, RECOVERY_RELABELLED],
        )

    def test_a_crash_at_the_relabel_is_recovered(self) -> None:
        # The finish recorded that it announced itself and never routed, so
        # the reviewer was left on the stage the rebase ran from with the
        # anchor still pinned. The tick that comes back owes the route and the
        # write, and nothing else.
        self._crashes(self._dies_at_the_relabel())

        self._refreshes()

        self._assert_settled()
        self._assert_reviewable()
        self.assertEqual(
            [record[METHOD_FIELD] for record in events_of(self, EVENT_REBASED)],
            [CLEAN_REBASE],
        )

    def test_a_crash_after_the_route_is_recovered(self) -> None:
        # The last window: the reviewer has been routed and the write that
        # clears the record has not. Nothing is announced twice.
        self._crashes(self._dies_after_the_relabel())

        self._refreshes()

        self._assert_settled()
        self._assert_reviewable()
        self.assertEqual(
            [record[METHOD_FIELD] for record in events_of(self, EVENT_REBASED)],
            [CLEAN_REBASE],
        )

    def test_a_rotation_follows_a_settled_one(self) -> None:
        # A settled transfer is never cleared, so it is still standing when
        # the next base advance anchors its own rebase to the very commit
        # that transfer rotated onto. Read as a claim about the new attempt
        # it is a permission leased to the previous one -- unvouchable -- and
        # the tick that has not even rebased yet parks instead of retrying.
        self._refreshes()
        rotated = self._wt_head()
        self._advances_the_base_again()
        self._crashes(self._dies_before_the_rebase())

        self._refreshes()

        self._assert_rotated_again(rotated)

    def _dies_before_the_push_returns(self):
        """The request reaches the remote and its answer never comes back."""
        return _LandsThenDies(self._gh)

    def _assert_rotated_again(self, rotated: str) -> None:
        """The second replay carries the verdict the first one earned."""
        replayed = self._wt_head()
        self.assertNotEqual(replayed, rotated)
        durable = self._durable()
        self.assertFalse(durable.get(KEY_AWAITING_HUMAN))
        self._assert_rotated_onto(durable, replayed)
        # Two rotations, and still one reading and one verdict: the second
        # replay is licensed by the permit rather than adjudicated afresh.
        self.assertEqual(len(events_of(self, EVENT_TRANSFER)), 2)
        self._assert_decided_once()
        self._assert_reviewable()

    def _assert_left_a_replay_nothing_names(self) -> None:
        """The premise of the window between `git rebase` and its record."""
        durable = self._durable()
        self.assertNotEqual(self._wt_head(), self.accepted)
        published = self._gh.pulls[PR_NUMBER].head
        self.assertEqual(published.sha, self.accepted)
        self.assertEqual(durable.get(KEY_PENDING_PUSH_SHA), self.accepted)
        self.assertIsNone(durable.get(KEY_PENDING_REWRITE_SHA))

    def _assert_finished(self, pusher, method: str) -> None:
        """The verdict is on the replay and the reviewer has been sent to it."""
        self._assert_settled()
        self._assert_reviewable()
        self.assertEqual(len(events_of(self, EVENT_REBASED)), 1)
        self.assertEqual(
            events_of(self, EVENT_REBASED)[0][METHOD_FIELD], method,
        )
        self.assertFalse(pusher.revision and pusher.revision != self._wt_head())

    def _assert_settled(self) -> None:
        """One adjudication, one transfer, and the exemption on the replay."""
        replayed = self._wt_head()
        self.assertNotEqual(replayed, self.accepted)
        durable = self._durable()
        self.assertTrue(_exemption.is_exempt(durable, replayed))
        self.assertEqual(
            _rewrites.read_rewrite_authorization(durable).phase,
            _rewrites.LateRewritePhase.PUBLISHED,
        )
        self.assertEqual(len(events_of(self, EVENT_TRANSFER)), 1)
        self._assert_decided_once()

    def _assert_rotated_onto(self, durable, published: str) -> None:
        """The verdict is on this commit and its permission is spent."""
        self.assertTrue(_exemption.is_exempt(durable, published))
        self.assertEqual(
            _rewrites.read_rewrite_authorization(durable).phase,
            _rewrites.LateRewritePhase.PUBLISHED,
        )

    def _assert_decided_once(self) -> None:
        """One reading, one verdict, and one trip through the adjudication."""
        self.assertEqual(len(events_of(self, EVENT_MEASUREMENT)), 1)
        self.assertEqual(len(events_of(self, EVENT_VERDICT)), 1)
        self.assertEqual(
            self._gh.label_history.count((ISSUE, LABEL_DECOMPOSING)), 1,
        )

    def _assert_reviewable(self) -> None:
        """The record of the attempt is gone and the round is the reviewer's."""
        durable = self._durable()
        self.assertIsNone(durable.get(KEY_PENDING_PUSH_SHA))
        self.assertIsNone(durable.get(KEY_PENDING_REWRITE_SHA))
        self.assertEqual(durable.get(KEY_REVIEW_ROUND), 0)
        self.assertIn((ISSUE, LABEL_VALIDATING), self._gh.label_history)
