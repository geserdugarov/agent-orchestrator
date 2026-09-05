# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Crash recovery against a real repository left mid-rebase."""

from __future__ import annotations

import unittest

from tests.git.base_sync import recovery_git_support as fixtures
from tests.git.base_sync.recovery_git_support import RecoveryGitFixtureMixin


class RecoveryRealGitTest(RecoveryGitFixtureMixin, unittest.TestCase):
    """The comparison the routing runs on is the one git itself computed."""

    def test_unpushed_rebase_is_leased_onto_remote(self) -> None:
        # A rebase replays the branch, so the commit the pull request still
        # carries is on no local history: git counts this branch as behind its
        # own publication as well as ahead of it. What says the push never
        # went out is the remote standing EXACTLY on the anchor the rebase
        # pinned before git ran, and the counts would say the opposite.
        ahead, behind = self.divergence_from_remote()
        self.assertGreater(behind, 0)
        self.assertGreater(ahead, 0)

        recovered = self.recover()

        # So the recovery reissues the push the crash interrupted rather than
        # parking the branch as one somebody else moved.
        self.assertTrue(recovered)
        self.assertEqual(self.push.leases, [self.anchor])
        self.assertEqual(self._remote_head(), self.recovered)
        self.assertEqual(fixtures.head_sha(self.work), self.recovered)
        self._assert_routed_to_validating("crash_recovery_pushed")

    def test_landed_push_is_finalized_once(self) -> None:
        self.publish_recovered_head()

        recovered = self.recover()

        # Only the recovery's own fetch can tell that the interrupted push
        # already landed -- the stale tracking ref still names the anchor.
        self.assertTrue(recovered)
        self.assertEqual(self.push.leases, [])
        self.assertEqual(self._remote_head(), self.recovered)
        self._assert_routed_to_validating("crash_recovery_relabel_only")

    def test_an_unrecorded_divergence_is_not_pushed(self) -> None:
        # The remote is exactly where the crash left it and the checkout is
        # clean and diverged -- the shape a replay has, and the shape a
        # worktree somebody rebuilt has too. Nothing on the comment says this
        # commit is the attempt's own, so the lease the anchor would satisfy
        # is never spent and the candidate stays on the pull request.
        stranded = self.strand_an_unrelated_head()
        ahead, behind = self.divergence_from_remote()
        self.assertEqual((ahead, behind), (1, 1))

        recovered = self.recover()

        self.assertTrue(recovered)
        self.assertEqual(self.push.leases, [])
        self.assertEqual(self._remote_head(), self.anchor)
        self.assertNotEqual(stranded, self.recovered)
        self._assert_parked(fixtures.PARK_PUSH_FAILED)

    def test_out_of_band_update_restores_anchor(self) -> None:
        pushed_elsewhere = self.advance_remote_out_of_band()

        recovered = self.recover()

        # Ahead *and* behind: the reissued force-push would drop someone
        # else's commit, so HEAD goes back to the last-known remote head.
        self.assertTrue(recovered)
        self.assertEqual(self.push.leases, [])
        self.assertEqual(self._remote_head(), pushed_elsewhere)
        self._assert_parked(fixtures.PARK_PUSH_FAILED)

    def test_dirty_worktree_is_reset_and_cleaned(self) -> None:
        (self.work / fixtures.FEATURE_FILE).write_text("half-resolved\n")
        (self.work / fixtures.SCRATCH_FILE).write_text("scratch\n")

        recovered = self.recover()

        # A rebase that left edits behind never had a publishable head, so
        # the worktree is restored and the leftovers are discarded.
        self.assertTrue(recovered)
        self.assertEqual(self.push.leases, [])
        self.assertEqual(self._remote_head(), self.anchor)
        self.assertFalse((self.work / fixtures.SCRATCH_FILE).exists())
        self.assertTrue(self.is_clean())
        self._assert_parked(fixtures.PARK_DIRTY)

    def _remote_head(self) -> str:
        return fixtures.head_sha(self.remote, fixtures.BRANCH_REF)

    def _assert_routed_to_validating(self, method: str) -> None:
        self.assertIn(
            (fixtures.ISSUE, fixtures.VALIDATING), self.gh.label_history,
        )
        published = self.gh.pinned_data(fixtures.ISSUE)
        self.assertIsNone(published.get(fixtures.KEY_PENDING_PUSH_SHA))
        self.assertEqual(
            [
                event.get(fixtures.METHOD_FIELD)
                for event in self.rebase_events()
            ],
            [method],
        )

    def _assert_parked(self, reason: str) -> None:
        # The reset put HEAD back on the anchor, so the anchor is dropped and
        # a later tick re-enters through the normal rebase flow instead.
        self.assertEqual(fixtures.head_sha(self.work), self.anchor)
        published = self.gh.pinned_data(fixtures.ISSUE)
        self.assertTrue(published.get(fixtures.KEY_AWAITING_HUMAN))
        self.assertEqual(published.get(fixtures.KEY_PARK_REASON), reason)
        self.assertIsNone(published.get(fixtures.KEY_PENDING_PUSH_SHA))
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.rebase_events(), [])


if __name__ == "__main__":
    unittest.main()
