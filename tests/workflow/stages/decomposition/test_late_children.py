# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a split's children are born knowing, and the order they are created in."""
from __future__ import annotations

import unittest
from dataclasses import replace
from types import MappingProxyType

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
from tests.support.fakes import make_issue
from tests.workflow.fixtures import LABEL_BLOCKED, LABEL_DONE
from tests.workflow.stages.decomposition.late_crash_support import (
    killed_after,
    recording_children,
    refusing,
    refusing_child_writes,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    CHILDREN,
    KEY_CHILDREN,
    KEY_CONSUMERS,
    KEY_DEP_GRAPH,
    KEY_EXPECTED_CHILDREN,
    KEY_PARENT_NUMBER,
    KEY_UMBRELLA,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    PARK_CHILDREN_FAILED,
    SNAPSHOT_REF,
    LateSplitCase,
    ancestry_of,
    first_child,
    label_of,
    sibling_marker,
)

RESOURCE_CHILD = "child"

STATE_PENDING = "pending"

BASE_BRANCH = "main"

CHERRY_PICK = "git cherry-pick"

COPY_PATHS = "git checkout"

NO_HUNK_SPLITTING = "Do **not** split hunks mechanically"

# The children an earlier decomposition of this same issue left behind, and
# the graph it recorded over them.
_DONE_CHILD = 900

_PRIOR_MANIFEST = MappingProxyType({
    "children": [_DONE_CHILD, _DONE_CHILD + 1],
    "dep_graph": {"1": [0]},
    "decomposed_at": "2026-01-01T00:00:00Z",
})

# A child marker naming a generation this transaction is not running.
_FOREIGN_MARKER = (
    f"<!--orchestrator-late-child:issue={LATE_ISSUE_NUMBER}"
    ":cycle=1:generation=1:index=0-->"
)

# Another issue entirely, adjudicating under the same cycle and generation --
# which is the ordinary case, not a contrived one, since a cycle is minted per
# issue.
OTHER_PARENT = 77

# Every depth automatic splitting is allowed from, paired with the depth the
# children it creates are born at. Depth 3 is absent because it may not split
# at all, which is the bound's own test below.
DEPTHS = ((0, 1), (1, 2), (2, 3))


class SplitChildrenCase(LateSplitCase):
    """A case that reads the register this generation records its children on."""

    def _recorded(self) -> list:
        """The child numbers the parent records for this generation."""
        return self._pinned().get(KEY_CHILDREN) or []


class ChildCreationOrderTest(SplitChildrenCase, unittest.TestCase):
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

        self._resume()

        self.assertEqual(self.github.created_child_issues, created)
        self.assertEqual(
            self._recorded(), [child.number for child in created],
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
        self.assertEqual(self._recorded(), [created])
        self.assertEqual(self._pinned()[KEY_CONSUMERS], [created])

    def test_a_resumed_walk_records_no_fewer(self) -> None:
        # A resumed pass rebuilds the recorded list as it goes, and a write
        # that dropped back to what it had placed so far would leave a crash
        # in the middle of it with orphans the parent no longer knows about.
        self._transact()
        recorded = list(self._recorded())
        widths = []
        with recording_children(self.github, widths):
            self._resume()

        self.assertEqual(recorded, self._recorded())
        self.assertTrue(
            all(len(width) >= len(recorded) for width in widths),
            f"the recorded list narrowed mid-walk: {widths}",
        )


class PriorDecompositionTest(SplitChildrenCase, unittest.TestCase):
    """An earlier decomposition's children are not this split's to adopt.

    The shape that produces one: an issue is decomposed, its children resolve,
    the parent flips back to `ready`, implements, and its committed candidate
    turns out to be oversized. It still carries the earlier `children` list
    and dependency graph the whole time.
    """

    def setUp(self) -> None:
        super().setUp()
        self.settled = [_DONE_CHILD, _DONE_CHILD + 1]
        for number in self.settled:
            self.github.add_issue(make_issue(number, label=LABEL_DONE))
        self.github.seed_state(
            self.issue.number, **{**self._pinned(), **_PRIOR_MANIFEST},
        )

    def test_it_creates_its_own_children_instead(self) -> None:
        # Adopting them would reseed and reactivate completed issues, each
        # under an ancestry it has nothing to do with.
        self._transact()

        created = [child.number for child in self.github.created_child_issues]
        self.assertEqual(len(created), len(CHILDREN))
        self.assertNotIn(_DONE_CHILD, created)
        self.assertEqual(self._recorded(), created)

    def test_it_leaves_the_settled_children_alone(self) -> None:
        self._transact()

        for number in self.settled:
            with self.subTest(child=number):
                self.assertEqual(
                    label_of(self.github, number), LABEL_DONE,
                )
                self.assertEqual(self.github.pinned_data(number), {})

    def test_the_earlier_graph_does_not_survive(self) -> None:
        # A graph indexed against another manifest would hold this split's
        # children behind dependencies that are not theirs.
        self._transact(children=({"title": "A", "body": "only slice"},))

        self.assertIsNone(self._pinned().get(KEY_DEP_GRAPH))


class OrphanAdoptionTest(SplitChildrenCase, unittest.TestCase):
    """A child created into a crash is adopted, never opened twice."""

    def test_an_unrecorded_child_is_adopted(self) -> None:
        # The one window the ordered register cannot close on its own: the
        # create returned and nothing outside GitHub knows the number.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_after(self.github, "create_child_issue"),
            )
        orphan = self.github.created_child_issues[0].number

        resumed = self._resume()

        self.assertEqual(resumed.disposition, _LateDisposition.SETTLED)
        self.assertEqual(
            len(self.github.created_child_issues), len(CHILDREN),
        )
        self.assertEqual(self._recorded()[0], orphan)

    def test_an_unreadable_lookup_creates_nothing(self) -> None:
        # "Could not ask" read as "there is no orphan" is what opens a second
        # issue for a slice that already has one, so the walk parks instead.
        with refusing(self.github, "find_issue_carrying"):
            with self.assertLogs(level="ERROR"):
                outcome = self._transact()

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(self.github.created_child_issues, [])
        self.assertEqual(
            self._pinned().get(KEYS.park_reason), PARK_CHILDREN_FAILED,
        )

    def test_another_generation_is_not_adopted(self) -> None:
        # The marker names the adjudication and the slice, so a child of some
        # earlier generation is not this one's to take over.
        stranger = self.github.create_child_issue(
            title="A", body=_FOREIGN_MARKER, parent_number=self.issue.number,
            labels=[LABEL_BLOCKED],
        )

        self._transact()

        self.assertNotIn(
            stranger.number, self._recorded(),
        )

    def test_another_parent_s_child_is_not_adopted(self) -> None:
        # A cycle identity is minted per issue and repeats across them: two
        # parents adjudicating their first candidate are both cycle 1. The
        # lookup walks a workflow label rather than one parent's children, so
        # without the issue in the marker one parent would adopt, reseed, and
        # activate the other's child.
        sibling = self.github.create_child_issue(
            title="A",
            body=sibling_marker(self.generation, OTHER_PARENT),
            parent_number=OTHER_PARENT,
            labels=[LABEL_BLOCKED],
        )

        self._transact()

        self.assertNotIn(sibling.number, self._recorded())
        self.assertEqual(
            len(self.github.created_child_issues), len(CHILDREN) + 1,
        )
        self.assertEqual(self.github.pinned_data(sibling.number), {})


class ChildInheritanceTest(SplitChildrenCase, unittest.TestCase):
    """A child is seeded with the lineage and the snapshot it may reuse."""

    def test_it_carries_the_ancestry_it_will_read(self) -> None:
        seeded = self._seeded_ancestry()

        self.assertEqual(seeded.root_issue, ROOT_ISSUE)
        self.assertEqual(seeded.parent_issue, LATE_ISSUE_NUMBER)
        self.assertEqual(seeded.cycle_id, CYCLE_ID)
        self.assertEqual(seeded.generation, GENERATION_NUMBER)
        self.assertEqual(seeded.base_branch, BASE_BRANCH)

    def test_it_carries_the_parent_link(self) -> None:
        self._transact()

        self.assertEqual(
            self._child_state(self._first())[KEY_PARENT_NUMBER],
            LATE_ISSUE_NUMBER,
        )

    def test_it_carries_the_snapshot_and_commit(self) -> None:
        seeded = self._seeded_ancestry()

        self.assertEqual(seeded.snapshot_ref, SNAPSHOT_REF)
        self.assertEqual(seeded.snapshot_sha, CANDIDATE_SHA)

    def test_a_child_carries_its_declared_scope(self) -> None:
        # What its own late prompt states as the scope, so an indivisible
        # slice is judged against the words the adjudication wrote rather than
        # against an issue body somebody has since edited.
        self._transact()

        self.assertEqual(
            [
                ancestry_of(self.github, child.number).scope
                for child in self.github.created_child_issues
            ],
            [child["body"] for child in CHILDREN],
        )

    def test_a_re_seed_leaves_child_work_alone(self) -> None:
        # By the time a retry reaches a child that was already created, that
        # child may be implementing.
        self._transact()
        number = self._first()
        self.github.seed_state(
            number, **self._child_state(number), dev_agent="codex",
        )

        self._resume()

        self.assertEqual(self._child_state(number)["dev_agent"], "codex")

    def _first(self) -> int:
        """The number of the child that owns the manifest's first slice."""
        return first_child(self.github).number

    def _seeded_ancestry(self):
        """Split once, and read what the first child was seeded with."""
        self._transact()
        return ancestry_of(self.github, self._first())


class ChildBodyTest(SplitChildrenCase, unittest.TestCase):
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


class LineageDepthTest(SplitChildrenCase, unittest.TestCase):
    """Children are born one deeper, and the bound is enforced here too."""

    def test_a_child_is_born_one_deeper(self) -> None:
        for depth, born_at in DEPTHS:
            with self.subTest(depth=depth):
                self.setUp()
                deeper = replace(self.generation, lineage_depth=depth)

                self._transact(generation=deeper)

                child = first_child(self.github)
                self.assertEqual(
                    ancestry_of(self.github, child.number).lineage_depth,
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
