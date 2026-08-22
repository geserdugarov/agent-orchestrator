# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a split's children are born knowing, and the order they are created in."""
from __future__ import annotations

import unittest
from dataclasses import replace

from orchestrator.workflow.late_split.models import MAX_LINEAGE_DEPTH
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateDisposition,
)

from tests.workflow.stages.decomposition.late_test_support import (
    CANDIDATE_SHA,
    CYCLE_ID,
    GENERATION_NUMBER,
    KEYS,
    LATE_ISSUE_NUMBER,
    ROOT_ISSUE,
)
from tests.workflow.stages.decomposition.late_crash_support import (
    recording_children,
    refusing,
    refusing_child_writes,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    KEY_ANCESTRY_BASE,
    KEY_ANCESTRY_CYCLE,
    KEY_ANCESTRY_DEPTH,
    KEY_ANCESTRY_GENERATION,
    KEY_ANCESTRY_PARENT,
    KEY_ANCESTRY_REF,
    KEY_ANCESTRY_ROOT,
    KEY_ANCESTRY_SHA,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    CHILDREN,
    KEY_CHILDREN,
    KEY_CONSUMERS,
    KEY_DECLARED_SCOPE,
    KEY_DEP_GRAPH,
    KEY_EXPECTED_CHILDREN,
    KEY_PARENT_NUMBER,
    KEY_UMBRELLA,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    PARK_CHILDREN_FAILED,
    SNAPSHOT_REF,
    LateSplitCase,
    first_child,
)

RESOURCE_CHILD = "child"

STATE_PENDING = "pending"

BASE_BRANCH = "main"

CHERRY_PICK = "git cherry-pick"

COPY_PATHS = "git checkout"

NO_HUNK_SPLITTING = "Do **not** split hunks mechanically"

# Every depth automatic splitting is allowed from, paired with the depth the
# children it creates are born at. Depth 3 is absent because it may not split
# at all, which is the bound's own test below.
DEPTHS = ((0, 1), (1, 2), (2, 3))


class ChildCreationOrderTest(LateSplitCase, unittest.TestCase):
    """The parent knows what it is making before it makes any of it."""

    def test_the_umbrella_precedes_every_child(self) -> None:
        # What a tick that died mid-loop is read back through: the count tells
        # a partial split from a finished one, and the flag says the parent has
        # no implementation of its own to return to.
        self._transact()

        pinned = self._pinned()
        self.assertEqual(pinned[KEY_EXPECTED_CHILDREN], len(CHILDREN))
        self.assertTrue(pinned[KEY_UMBRELLA])

    def test_a_child_is_recorded_as_a_consumer(self) -> None:
        # A child recorded as one and not the other is a child the snapshot
        # would stop waiting for before it is done.
        self._transact()

        created = [child.number for child in self.github.created_child_issues]
        pinned = self._pinned()
        self.assertEqual(pinned[KEY_CHILDREN], created)
        self.assertEqual(pinned[KEY_CONSUMERS], sorted(created))
        for number in created:
            self.assertEqual(
                self._resources()[(RESOURCE_CHILD, str(number))],
                STATE_PENDING,
            )

    def test_the_dependency_graph_travels_with_them(self) -> None:
        self._transact()

        self.assertEqual(self._pinned()[KEY_DEP_GRAPH], {"1": [0]})

    def test_a_recorded_child_is_adopted(self) -> None:
        # The retry a crash after the first child leaves: the parent's own
        # recorded list is the register, so no slice gets a second issue.
        self._transact()
        created = list(self.github.created_child_issues)

        self._transact()

        self.assertEqual(self.github.created_child_issues, created)
        self.assertEqual(
            self._pinned()[KEY_CHILDREN], [child.number for child in created],
        )

    def test_a_refused_creation_records_what_exists(self) -> None:
        with refusing(self.github, "create_child_issue"):
            with self.assertLogs(level="ERROR"):
                outcome = self._transact()

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(
            self._pinned().get(KEYS.park_reason), PARK_CHILDREN_FAILED,
        )
        self.assertEqual(self._pinned()[KEY_EXPECTED_CHILDREN], len(CHILDREN))

    def test_a_refused_seed_keeps_the_record(self) -> None:
        # The child exists on GitHub by then, so the parent must already know
        # about it -- otherwise the retry creates a second one beside it.
        with refusing_child_writes(self.github):
            with self.assertLogs(level="ERROR"):
                outcome = self._transact()

        created = first_child(self.github).number
        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(self._pinned()[KEY_CHILDREN], [created])
        self.assertEqual(self._pinned()[KEY_CONSUMERS], [created])

    def test_a_resumed_walk_records_no_fewer(self) -> None:
        # A resumed pass rebuilds the recorded list as it goes, and a write
        # that dropped back to what it had placed so far would leave a crash
        # in the middle of it with orphans the parent no longer knows about.
        self._transact()
        recorded = list(self._pinned()[KEY_CHILDREN])
        widths = []
        with recording_children(self.github, widths):
            self._transact()

        self.assertEqual(recorded, self._pinned()[KEY_CHILDREN])
        self.assertTrue(
            all(len(width) >= len(recorded) for width in widths),
            f"the recorded list narrowed mid-walk: {widths}",
        )


class ChildInheritanceTest(LateSplitCase, unittest.TestCase):
    """A child is seeded with the lineage and the snapshot it may reuse."""

    def test_it_carries_the_ancestry_it_will_read(self) -> None:
        self._transact()
        child = first_child(self.github)

        seeded = self._child_state(child.number)

        self.assertEqual(seeded[KEY_ANCESTRY_ROOT], ROOT_ISSUE)
        self.assertEqual(seeded[KEY_ANCESTRY_PARENT], LATE_ISSUE_NUMBER)
        self.assertEqual(seeded[KEY_ANCESTRY_CYCLE], CYCLE_ID)
        self.assertEqual(seeded[KEY_ANCESTRY_GENERATION], GENERATION_NUMBER)
        self.assertEqual(seeded[KEY_ANCESTRY_BASE], BASE_BRANCH)
        self.assertEqual(seeded[KEY_PARENT_NUMBER], LATE_ISSUE_NUMBER)

    def test_it_carries_the_snapshot_and_commit(self) -> None:
        self._transact()
        child = first_child(self.github)

        seeded = self._child_state(child.number)

        self.assertEqual(seeded[KEY_ANCESTRY_REF], SNAPSHOT_REF)
        self.assertEqual(seeded[KEY_ANCESTRY_SHA], CANDIDATE_SHA)

    def test_a_child_carries_its_declared_scope(self) -> None:
        # What its own late prompt states as the scope, so an indivisible
        # slice is judged against the words the adjudication wrote rather than
        # against an issue body somebody has since edited.
        self._transact()

        self.assertEqual(
            [
                self._child_state(child.number)[KEY_DECLARED_SCOPE]
                for child in self.github.created_child_issues
            ],
            [child["body"] for child in CHILDREN],
        )

    def test_a_re_seed_leaves_child_work_alone(self) -> None:
        # By the time a retry reaches a child that was already created, that
        # child may be implementing.
        self._transact()
        child = first_child(self.github)
        self.github.seed_state(
            child.number, **self._child_state(child.number), dev_agent="codex",
        )

        self._transact()

        self.assertEqual(
            self._child_state(child.number)["dev_agent"], "codex",
        )


class ChildBodyTest(LateSplitCase, unittest.TestCase):
    """A child's body says where the work is and how it may be reused."""

    def test_it_opens_on_the_declared_slice(self) -> None:
        self.assertTrue(self._body().startswith(CHILDREN[0]["body"]))

    def test_it_names_the_snapshot_and_both_commits(self) -> None:
        body = self._body()

        self.assertIn(SNAPSHOT_REF, body)
        self.assertIn(CANDIDATE_SHA, body)
        self.assertIn(self.generation.base_sha, body)
        self.assertIn(BASE_BRANCH, body)

    def test_it_offers_reuse_and_forbids_hunks(self) -> None:
        # File and hunk boundaries do not express issue scope, so the
        # judgment about what belongs to a slice stays with the developer.
        body = self._body()

        self.assertIn(CHERRY_PICK, body)
        self.assertIn(COPY_PATHS, body)
        self.assertIn(NO_HUNK_SPLITTING, body)

    def _body(self) -> str:
        """The body the first slice's child was opened with."""
        self._transact()
        return first_child(self.github).body


class LineageDepthTest(LateSplitCase, unittest.TestCase):
    """Children are born one deeper, and the bound is enforced here too."""

    def test_a_child_is_born_one_deeper(self) -> None:
        for depth, born_at in DEPTHS:
            with self.subTest(depth=depth):
                self.setUp()
                deeper = replace(self.generation, lineage_depth=depth)

                self._transact(generation=deeper)

                child = first_child(self.github)
                self.assertEqual(
                    self._child_state(child.number)[KEY_ANCESTRY_DEPTH],
                    born_at,
                )

    def test_the_bound_creates_nothing(self) -> None:
        # A structurally perfect split at the bound is refused where the
        # children would be born as well as where the reply was parsed.
        at_bound = replace(
            self.generation, lineage_depth=MAX_LINEAGE_DEPTH,
        )

        outcome = self._transact(generation=at_bound)

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(self.github.created_child_issues, [])
        self.assertIsNone(self._pinned().get(KEY_CHILDREN))

    def test_an_unknown_depth_creates_nothing(self) -> None:
        # A lineage that cannot say how deep it is cannot show it has room.
        unknown = replace(self.generation, lineage_depth=None)

        outcome = self._transact(generation=unknown)

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(self.github.created_child_issues, [])


if __name__ == "__main__":
    unittest.main()
