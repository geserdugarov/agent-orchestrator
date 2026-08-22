# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Real git: what create, prove, and delete do to a ref that is really there.

Every assertion below is made against a bare repository on disk rather than
against recorded argv, because what is under test is what git DOES with the
refspecs and leases the transport builds. A recorder would happily confirm a
`--force-with-lease` that git rejects, a refspec no server accepts, and a fetch
that brings nothing back.
"""

from __future__ import annotations

import unittest

from orchestrator.git.snapshots import namespace, refs

from tests.git.snapshots.snapshot_test_support import PLUMBING_LOG, real_remote

ISSUE = 41
CYCLE = 3
GENERATION = 1

REF = "refs/orchestrator/late-split/issue-41/cycle-3/gen-1"

FOREIGN_REF = "refs/heads/main"

ERROR = "ERROR"


def _create(remote, *, ref: str = REF) -> refs.SnapshotOutcome:
    """Create this remote's snapshot at the commit it froze."""
    return refs.create_snapshot_ref(
        remote.spec, remote.clone, ref=ref, sha=remote.sha,
    )


def _prove(remote, *, ref: str = REF) -> refs.SnapshotOutcome:
    """Fetch that snapshot back and resolve it here."""
    return refs.prove_snapshot_ref(
        remote.spec, remote.clone, ref=ref, sha=remote.sha,
    )


def _delete(remote, *, ref: str = REF, sha=None) -> refs.SnapshotOutcome:
    """Reclaim that snapshot, named against the commit it preserved."""
    return refs.delete_snapshot_ref(
        remote.spec, remote.clone, ref=ref, sha=sha or remote.sha,
    )


class SnapshotCreateTest(unittest.TestCase):
    """A snapshot is written once, and never over something else."""

    def test_it_creates_the_ref_at_the_exact_commit(self) -> None:
        with real_remote() as remote:
            outcome = _create(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.CREATED)
            self.assertEqual(remote.remote_ref_sha(REF), remote.sha)

    def test_a_repeat_verifies_instead_of_writing(self) -> None:
        # The create-or-verify contract a crashed tick relies on: the ref it
        # already pushed is the answer, at the cost of one read.
        with real_remote() as remote:
            _create(remote)

            outcome = _create(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.PRESENT)
            self.assertEqual(remote.remote_ref_sha(REF), remote.sha)

    def test_an_occupied_ref_is_never_overwritten(self) -> None:
        # The one automatic alternative would be destroying the only copy of
        # somebody else's candidate, so the ref is left exactly as it is.
        with real_remote() as remote:
            remote.plant_ref(REF, remote.other_sha)

            with self.assertLogs(PLUMBING_LOG, level=ERROR):
                outcome = _create(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.MISMATCH)
            self.assertEqual(remote.remote_ref_sha(REF), remote.other_sha)

    def test_a_ref_outside_the_namespace_is_refused(self) -> None:
        with real_remote() as remote:
            base_before = remote.remote_ref_sha(FOREIGN_REF)

            with self.assertLogs(PLUMBING_LOG, level=ERROR):
                outcome = _create(remote, ref=FOREIGN_REF)

            self.assertEqual(outcome, refs.SnapshotOutcome.REFUSED)
            self.assertEqual(remote.remote_ref_sha(FOREIGN_REF), base_before)

    def test_an_unreachable_remote_says_nothing(self) -> None:
        # "Nobody could say" is not "there is nothing there": a create on the
        # strength of it would push into a namespace nobody had read.
        with real_remote(reachable=False) as remote:
            with self.assertLogs(PLUMBING_LOG, level=ERROR):
                outcome = _create(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.UNREADABLE)


class SnapshotProofTest(unittest.TestCase):
    """A snapshot is proved by fetching it, not by asking about it."""

    def test_a_created_ref_fetches_back_and_resolves(self) -> None:
        with real_remote() as remote:
            _create(remote)

            outcome = _prove(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.PROVEN)

    def test_a_ref_the_remote_lacks_is_refused(self) -> None:
        with real_remote() as remote:
            with self.assertLogs(PLUMBING_LOG, level=ERROR):
                outcome = _prove(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.REFUSED)

    def test_another_commit_under_it_is_a_mismatch(self) -> None:
        # The sharp case: what landed locally is not the candidate the
        # children would be told to cut from.
        with real_remote() as remote:
            remote.plant_ref(REF, remote.other_sha)

            with self.assertLogs(PLUMBING_LOG, level=ERROR):
                outcome = _prove(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.MISMATCH)

    def test_a_ref_outside_the_namespace_is_refused(self) -> None:
        with real_remote() as remote:
            with self.assertLogs(PLUMBING_LOG, level=ERROR):
                outcome = _prove(remote, ref=FOREIGN_REF)

            self.assertEqual(outcome, refs.SnapshotOutcome.REFUSED)


class SnapshotDeleteTest(unittest.TestCase):
    """Reclamation is idempotent because an absent ref is a success."""

    def test_it_deletes_a_ref_it_can_see(self) -> None:
        with real_remote() as remote:
            remote.plant_ref(REF, remote.sha)

            outcome = _delete(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.DELETED)
            self.assertEqual(remote.remote_ref_sha(REF), "")

    def test_an_absent_ref_is_already_reclaimed(self) -> None:
        # The crash between the push that deleted a ref and the write that
        # would have recorded it: the retry has nothing to do and says so.
        with real_remote() as remote:
            remote.plant_ref(REF, remote.sha)
            _delete(remote)

            outcome = _delete(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.ABSENT)

    def test_another_commit_under_it_is_not_reclaimed(self) -> None:
        # The one operation whose blast radius is somebody else's content: a
        # ref re-pointed before the reclamation is not the artifact this
        # generation preserved, and deleting it would destroy theirs.
        with real_remote() as remote:
            remote.plant_ref(REF, remote.other_sha)

            with self.assertLogs(PLUMBING_LOG, level=ERROR):
                outcome = _delete(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.MISMATCH)
            self.assertEqual(remote.remote_ref_sha(REF), remote.other_sha)

    def test_a_never_created_ref_is_absent(self) -> None:
        with real_remote() as remote:
            outcome = _delete(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.ABSENT)

    def test_a_ref_outside_the_namespace_survives(self) -> None:
        # Deleting one would destroy an artifact this domain never created.
        with real_remote() as remote:
            with self.assertLogs(PLUMBING_LOG, level=ERROR):
                outcome = _delete(remote, ref=FOREIGN_REF)

            self.assertEqual(outcome, refs.SnapshotOutcome.REFUSED)
            self.assertEqual(
                remote.remote_ref_sha(FOREIGN_REF), remote.other_sha,
            )

    def test_an_unreachable_remote_deletes_nothing(self) -> None:
        with real_remote(reachable=False) as remote:
            with self.assertLogs(PLUMBING_LOG, level=ERROR):
                outcome = _delete(remote)

            self.assertEqual(outcome, refs.SnapshotOutcome.UNREADABLE)


class SnapshotRoundTripTest(unittest.TestCase):
    """The whole lifecycle the capability check has to prove before rollout."""

    def test_create_prove_and_delete_hold_together(self) -> None:
        built = namespace.snapshot_ref(
            issue_number=ISSUE, cycle_id=CYCLE, generation=GENERATION,
        )
        with real_remote() as remote:
            self.assertEqual(
                _create(remote, ref=built),
                refs.SnapshotOutcome.CREATED,
            )
            self.assertEqual(
                _prove(remote, ref=built),
                refs.SnapshotOutcome.PROVEN,
            )
            self.assertEqual(
                _delete(remote, ref=built),
                refs.SnapshotOutcome.DELETED,
            )
            self.assertEqual(
                _delete(remote, ref=built),
                refs.SnapshotOutcome.ABSENT,
            )


if __name__ == "__main__":
    unittest.main()
