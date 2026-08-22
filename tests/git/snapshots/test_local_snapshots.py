# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Real git: where a fetched snapshot lands, and what reclaims it.

A remote ref is unique inside the repository that holds it, so three numbers
are enough there. The clone those repositories are fetched into is not: several
`REPOS` entries may share one `target_root` -- a single checkout with a public
and a private remote is the shape the per-issue branch namespace already exists
for -- and their ref stores are the same store. These cases drive two real
repositories through one clone and assert that neither ends up reading the
other's candidate.
"""

from __future__ import annotations

import unittest

from pathlib import Path

from orchestrator import config
from orchestrator.git.snapshots import namespace, refs

from tests.git.snapshots.snapshot_test_support import real_remote

REF = "refs/orchestrator/late-split/issue-41/cycle-3/gen-1"

# Where that ref lands once the first repository fetches it. The segment is the
# sanitized slug the per-issue branch namespace is built from.
MIRROR = (
    "refs/orchestrator/late-split-local/owner__repo/issue-41/cycle-3/gen-1"
)

# A slug longer than the segment a local ref may carry. Configuration bounds
# `owner/name` at nothing, so this is a shape an operator may really write.
_OVERLONG = namespace.MAX_REPOSITORY_SEGMENT * 3

LONG_SLUG = "owner/{0}".format("n" * _OVERLONG)


def _preserved(remote) -> None:
    """Create this repository's snapshot and fetch it back into the clone."""
    refs.create_snapshot_ref(
        remote.spec, remote.clone, ref=REF, sha=remote.sha,
    )
    refs.prove_snapshot_ref(
        remote.spec, remote.clone, ref=REF, sha=remote.sha,
    )


def _mirrored(remote) -> str:
    """What this repository's copy of the snapshot resolves to here."""
    return refs._local_ref_sha(
        remote.clone, refs.local_snapshot_ref(remote.spec, REF),
    )


class LocalSnapshotNameTest(unittest.TestCase):
    """The local name says which repository the snapshot came from."""

    def test_it_qualifies_the_remote_name(self) -> None:
        with real_remote() as remote:
            self.assertEqual(refs.local_snapshot_ref(remote.spec, REF), MIRROR)

    def test_a_fetched_snapshot_resolves_under_it(self) -> None:
        with real_remote() as remote:
            _preserved(remote)

            self.assertEqual(
                refs._local_ref_sha(remote.clone, MIRROR), remote.sha,
            )


class BoundedRepositoryTest(unittest.TestCase):
    """A slug configuration does not bound still produces a usable ref."""

    def test_a_long_slug_stays_short_and_unique(self) -> None:
        # Configuration imposes no length on `owner/name`, and a segment
        # merely truncated to fit would put two long-named repositories back
        # on one local ref -- so the rewrite carries the slug's own digest.
        near = f"{LONG_SLUG}x"

        first = refs.local_snapshot_ref(_spec_for(LONG_SLUG), REF)
        second = refs.local_snapshot_ref(_spec_for(near), REF)

        self.assertNotEqual(first, second)
        for built in (first, second):
            with self.subTest(ref=built):
                self.assertTrue(namespace.is_local_snapshot_ref(built))

    def test_a_long_slug_fetches_and_reclaims(self) -> None:
        # The failure this closes: creation succeeded and the proof raised
        # while building a name too long to be one, retried forever.
        with real_remote(slug=LONG_SLUG) as remote:
            _preserved(remote)

            self.assertEqual(_mirrored(remote), remote.sha)
            self.assertEqual(
                refs.delete_snapshot_ref(
                    remote.spec, remote.clone, ref=REF, sha=remote.sha,
                ),
                refs.SnapshotOutcome.DELETED,
            )
            self.assertIsNone(_mirrored(remote))


def _spec_for(slug: str) -> config.RepoSpec:
    """A spec naming one repository, for the local name it produces."""
    return config.RepoSpec(
        slug=slug, target_root=Path("/tmp"), base_branch="main",
    )


class SharedTargetRootTest(unittest.TestCase):
    """Two repositories sharing one clone do not share one local snapshot.

    An unqualified local name would have the second fetch force over the
    first, so a verification would answer for a candidate this call never saw
    -- and the child told to copy paths out of it would take them from the
    other repository's work.
    """

    def test_each_repository_fetches_onto_its_own_ref(self) -> None:
        with real_remote() as first:
            with real_remote(clone=first.clone) as second:
                _preserved(first)
                _preserved(second)

                self.assertNotEqual(
                    refs.local_snapshot_ref(first.spec, REF),
                    refs.local_snapshot_ref(second.spec, REF),
                )
                self.assertEqual(_mirrored(first), first.sha)
                self.assertEqual(_mirrored(second), second.sha)

    def test_each_proof_answers_for_its_own_candidate(self) -> None:
        # The failure an unqualified name produces is a false MISMATCH: the
        # ref the proof resolves carries the other repository's commit.
        with real_remote() as first:
            with real_remote(clone=first.clone) as second:
                _preserved(second)

                refs.create_snapshot_ref(
                    first.spec, first.clone, ref=REF, sha=first.sha,
                )

                self.assertEqual(
                    refs.prove_snapshot_ref(
                        first.spec, first.clone, ref=REF, sha=first.sha,
                    ),
                    refs.SnapshotOutcome.PROVEN,
                )

    def test_one_reclamation_leaves_the_other_alone(self) -> None:
        with real_remote() as first:
            with real_remote(clone=first.clone) as second:
                _preserved(first)
                _preserved(second)

                refs.delete_snapshot_ref(
                    first.spec, first.clone, ref=REF, sha=first.sha,
                )

                self.assertIsNone(_mirrored(first))
                self.assertEqual(_mirrored(second), second.sha)


class LocalSnapshotReclamationTest(unittest.TestCase):
    """This host's copy goes with the remote ref it mirrors."""

    def test_it_drops_this_host_s_copy_too(self) -> None:
        # A mirror nothing deletes holds the snapshot's objects against `gc`
        # for as long as the clone lives.
        with real_remote() as remote:
            _preserved(remote)
            self.assertEqual(
                refs._local_ref_sha(remote.clone, MIRROR), remote.sha,
            )

            refs.delete_snapshot_ref(
                remote.spec, remote.clone, ref=REF, sha=remote.sha,
            )

            self.assertIsNone(refs._local_ref_sha(remote.clone, MIRROR))

    def test_an_absent_remote_drops_a_stranded_copy(self) -> None:
        # The crash between the push that deleted a ref and the write that
        # would have recorded it leaves this host's copy behind.
        with real_remote() as remote:
            _preserved(remote)
            remote.drop_remote_ref(REF)

            self.assertEqual(
                refs.delete_snapshot_ref(
                    remote.spec, remote.clone, ref=REF, sha=remote.sha,
                ),
                refs.SnapshotOutcome.ABSENT,
            )
            self.assertIsNone(refs._local_ref_sha(remote.clone, MIRROR))


if __name__ == "__main__":
    unittest.main()
