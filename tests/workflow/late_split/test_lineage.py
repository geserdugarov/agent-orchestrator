# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a child born of a late split inherits, and its pinned round trip."""
from __future__ import annotations

import unittest
from types import MappingProxyType

from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import lineage as _lineage

SHA_LENGTH = 40

SNAPSHOT_SHA = "a" * SHA_LENGTH

SNAPSHOT_REF = "refs/orchestrator/late-split/issue-41/cycle-3/gen-1"

ROOT_ISSUE = 41

PARENT_ISSUE = 41

CYCLE_ID = 3

GENERATION = 1

DEPTH = 2

BASE_BRANCH = "main"

SCOPE = "the slice this child owns"

# What an issue that reached this workflow another way carries: none of these
# keys, so nothing about it reads as a lineage nobody recorded.
LEGACY_STATE = MappingProxyType({
    "dev_agent": "codex",
    "branch": "orchestrator/issue-7",
    "parent_number": 12,
})

# One damaged value per field contract. Every one of them would otherwise
# become a lineage this orchestrator never wrote: an identity that is not
# positive, a depth outside the bound, a commit that is not spelled like one,
# and a ref that names somebody else's branch.
DAMAGED_FIELDS = (
    ("late_ancestry_root_issue", 0),
    ("late_ancestry_parent", -1),
    ("late_ancestry_cycle_id", True),
    ("late_ancestry_generation", -1),
    ("late_ancestry_depth", 4),
    ("late_ancestry_snapshot_sha", "HEAD~1"),
    ("late_ancestry_snapshot_ref", "refs/heads/main"),
    ("late_ancestry_snapshot_ref", "refs/orchestrator/other/x"),
)


def ancestry(**overrides) -> _lineage.LateAncestry:
    """The ancestry a first-generation child is seeded with."""
    fields = {
        "root_issue": ROOT_ISSUE,
        "lineage_depth": DEPTH,
        "parent_issue": PARENT_ISSUE,
        "cycle_id": CYCLE_ID,
        "generation": GENERATION,
        "snapshot_ref": SNAPSHOT_REF,
        "snapshot_sha": SNAPSHOT_SHA,
        "base_branch": BASE_BRANCH,
        "scope": SCOPE,
    }
    return _lineage.LateAncestry(**{**fields, **overrides})


def round_trip(record: _lineage.LateAncestry) -> _lineage.LateAncestry:
    """What a written ancestry reads back as."""
    state = PinnedState()
    _lineage.write_late_ancestry(state, record)
    return _lineage.read_late_ancestry(state)


class AncestryRoundTripTest(unittest.TestCase):
    """Every field a child is seeded with survives the pinned comment."""

    def test_a_seeded_ancestry_reads_back_whole(self) -> None:
        self.assertEqual(round_trip(ancestry()), ancestry())

    def test_a_root_child_keeps_its_depth(self) -> None:
        # Depth 0 is a root of a lineage and is written as itself; reading it
        # back as absent would make a lineage unable to say how deep it is.
        self.assertEqual(round_trip(ancestry(lineage_depth=0)).lineage_depth, 0)

    def test_an_unknown_depth_is_not_a_root(self) -> None:
        # A child whose depth nobody recorded may not read back as one free to
        # split again.
        self.assertIsNone(
            round_trip(ancestry(lineage_depth=None)).lineage_depth,
        )

    def test_a_write_drops_an_earlier_record(self) -> None:
        # A child re-seeded against a snapshot that no longer exists must not
        # keep pointing at the old one.
        state = PinnedState()
        _lineage.write_late_ancestry(state, ancestry())

        _lineage.write_late_ancestry(state, ancestry(snapshot_ref=""))

        self.assertEqual(_lineage.read_late_ancestry(state).snapshot_ref, "")


class AncestryAbsenceTest(unittest.TestCase):
    """An issue that was never split into carries no lineage at all."""

    def test_a_legacy_issue_reads_back_absent(self) -> None:
        state = PinnedState(data=dict(LEGACY_STATE))

        read = _lineage.read_late_ancestry(state)

        self.assertFalse(read.is_present)
        self.assertFalse(read.has_snapshot)

    def test_an_absent_ancestry_adds_no_key(self) -> None:
        state = PinnedState(data=dict(LEGACY_STATE))

        _lineage.write_late_ancestry(state, _lineage.LateAncestry())

        self.assertEqual(state.data, dict(LEGACY_STATE))

    def test_clearing_leaves_every_other_stage_alone(self) -> None:
        state = PinnedState(data=dict(LEGACY_STATE))
        _lineage.write_late_ancestry(state, ancestry())

        _lineage.clear_late_ancestry(state)

        self.assertEqual(state.data, dict(LEGACY_STATE))


class AncestryFailClosedTest(unittest.TestCase):
    """A hand-edited field never becomes a lineage this binary acts on."""

    def test_a_damaged_field_reads_back_absent(self) -> None:
        for key, damaged in DAMAGED_FIELDS:
            with self.subTest(key=key, given=damaged):
                state = PinnedState()
                _lineage.write_late_ancestry(state, ancestry())
                state.set(key, damaged)

                read = _lineage.read_late_ancestry(state)

                self.assertNotEqual(getattr(read, _FIELDS[key]), _WHOLE[key])

    def test_a_snapshot_needs_both_halves(self) -> None:
        # A ref with no commit cannot be verified against anything, and a
        # commit with no ref names work nothing can fetch.
        self.assertFalse(round_trip(ancestry(snapshot_sha="")).has_snapshot)
        self.assertFalse(round_trip(ancestry(snapshot_ref="")).has_snapshot)
        self.assertTrue(round_trip(ancestry()).has_snapshot)

    def test_presence_needs_a_parent_and_a_cycle(self) -> None:
        self.assertTrue(round_trip(ancestry()).is_present)
        self.assertFalse(round_trip(ancestry(parent_issue=0)).is_present)
        self.assertFalse(round_trip(ancestry(cycle_id=0)).is_present)


# Which record field each pinned key answers on, and what a whole ancestry
# holds there, so a damaged value is asserted against the value it replaced.
_FIELDS = MappingProxyType({
    "late_ancestry_root_issue": "root_issue",
    "late_ancestry_parent": "parent_issue",
    "late_ancestry_cycle_id": "cycle_id",
    "late_ancestry_generation": "generation",
    "late_ancestry_depth": "lineage_depth",
    "late_ancestry_snapshot_sha": "snapshot_sha",
    "late_ancestry_snapshot_ref": "snapshot_ref",
})

_WHOLE = MappingProxyType({
    key: getattr(ancestry(), field) for key, field in _FIELDS.items()
})


if __name__ == "__main__":
    unittest.main()
