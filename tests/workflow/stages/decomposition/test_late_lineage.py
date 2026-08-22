# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""A lineage split three deep, each generation minted from the last one's seed.

The depths are walked for real rather than declared: every generation past the
root is built from the ancestry the previous transaction actually wrote onto
the child it created, so what is under test is the round trip -- what a split
seeds, and what a descendant's own adjudication then inherits from it. A test
that replaced `lineage_depth` by hand would prove the bound and nothing about
the propagation.
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import lineage as _lineage
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.late_split.models import (
    MAX_LINEAGE_DEPTH,
    LateGeneration,
)
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateDisposition,
)

from tests.support.fakes import FakeGitHubClient, make_issue
from tests.workflow.fixtures import LABEL_DECOMPOSING
from tests.workflow.stages.decomposition.late_test_support import (
    ADDITIONS,
    BASE_SHA,
    CANDIDATE_SHA,
    CYCLE_ID,
    ROOT_ISSUE,
    THRESHOLD,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    CHILDREN,
    KEY_CHILDREN,
    LateSplitCase,
    first_child,
)

# The one slice each generation is split into, so the lineage is a chain and
# the depth of the child under test is unambiguous.
_ONE_SLICE = (CHILDREN[0],)

_ROOT_SCOPE = "the whole of the root issue"


class NestedLineageTest(LateSplitCase, unittest.TestCase):
    """Root, depth 1, depth 2, and the refusal at the bound."""

    def setUp(self) -> None:
        super().setUp()
        self.generation = replace(
            self.generation, lineage_depth=0, scope=_ROOT_SCOPE,
        )

    def test_each_generation_inherits_the_seed(self) -> None:
        # The chain: every issue past the root is adjudicated under a
        # generation built from the ancestry the split before it wrote.
        for depth in range(MAX_LINEAGE_DEPTH):
            with self.subTest(depth=depth):
                parent_number = self.issue.number
                ancestry = self._split_once()

                self.assertEqual(ancestry.root_issue, ROOT_ISSUE)
                self.assertEqual(ancestry.lineage_depth, depth + 1)
                self.assertEqual(ancestry.parent_issue, parent_number)
                self.assertEqual(ancestry.scope, _ONE_SLICE[0]["body"])
                self.assertEqual(ancestry.snapshot_sha, CANDIDATE_SHA)

    def test_the_bound_refuses_what_it_seeded(self) -> None:
        # The deepest child a split may create sits exactly at the bound, and
        # the generation minted from its own ancestry may not split again.
        for generation in range(MAX_LINEAGE_DEPTH):
            self.assertLess(generation, MAX_LINEAGE_DEPTH)
            self._split_once()

        outcome = self._transact(children=_ONE_SLICE)

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(self.github.created_child_issues, [])

    def _split_once(self) -> _lineage.LateAncestry:
        """Split the issue under test, then become the child it created."""
        self._transact(children=_ONE_SLICE)
        child = first_child(self.github)
        ancestry = _lineage.read_late_ancestry(
            self.github.read_pinned_state(child),
        )
        self._become(child.number, ancestry)
        return ancestry

    def _become(self, number: int, ancestry: _lineage.LateAncestry) -> None:
        """Re-open this case on the child, under a generation from its seed.

        Exactly what a descendant's own size gate does: the lineage the
        generation is minted with comes off the ancestry the split wrote,
        never off a fresh count.
        """
        issue = make_issue(number, label=LABEL_DECOMPOSING)
        # Constructed WITH the issue so the fake's own numbering starts above
        # it: a child sharing its parent's number would read as an adoption.
        github = FakeGitHubClient([issue])
        self.generation = LateGeneration(
            cycle_id=CYCLE_ID,
            generation=1,
            root_issue=ancestry.root_issue,
            current_issue=number,
            lineage_depth=ancestry.lineage_depth,
            scope=ancestry.scope,
            candidate_sha=CANDIDATE_SHA,
            base_sha=BASE_SHA,
            threshold=THRESHOLD,
            additions=ADDITIONS,
        )
        seeded = PinnedState()
        _late_state.write_late_generation(seeded, self.generation)
        _lineage.write_late_ancestry(seeded, ancestry)
        github.seed_state(number, **seeded.data)
        self.github = github
        self.issue = issue


class ContradictedLineageTest(LateSplitCase, unittest.TestCase):
    """A generation minted without the ancestry may not create another one."""

    def setUp(self) -> None:
        super().setUp()
        self.ancestry = _lineage.LateAncestry(
            root_issue=ROOT_ISSUE,
            lineage_depth=MAX_LINEAGE_DEPTH - 1,
            parent_issue=ROOT_ISSUE,
            cycle_id=CYCLE_ID,
            generation=1,
        )

    def test_a_shallower_generation_creates_nothing(self) -> None:
        # A depth minted below the recorded one is exactly how a lineage buys
        # itself another generation past the bound.
        self._seed_ancestry()

        outcome = self._transact(
            generation=replace(self.generation, lineage_depth=0),
        )

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(self.github.created_child_issues, [])

    def test_a_foreign_root_creates_nothing(self) -> None:
        self._seed_ancestry()

        outcome = self._transact(
            generation=replace(
                self.generation,
                lineage_depth=MAX_LINEAGE_DEPTH - 1,
                root_issue=ROOT_ISSUE + 1,
            ),
        )

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)

    def test_an_agreeing_generation_splits(self) -> None:
        self._seed_ancestry()

        outcome = self._transact(
            generation=replace(
                self.generation, lineage_depth=MAX_LINEAGE_DEPTH - 1,
            ),
        )

        self.assertEqual(outcome.disposition, _LateDisposition.SETTLED)
        self.assertEqual(len(self._pinned()[KEY_CHILDREN]), len(CHILDREN))

    def test_no_ancestry_contradicts_nothing(self) -> None:
        outcome = self._transact()

        self.assertEqual(outcome.disposition, _LateDisposition.SETTLED)

    def _seed_ancestry(self) -> None:
        """Record the lineage this issue was created under."""
        seeded = self.github.read_pinned_state(self.issue)
        _lineage.write_late_ancestry(seeded, self.ancestry)
        self.github.seed_state(self.issue.number, **seeded.data)


if __name__ == "__main__":
    unittest.main()
