# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a split leaves behind when it dies while creating its children.

The middle of the transaction, and the part with a window nothing durable can
close on its own: `create_child_issue` returns and the process dies before the
write that records the number, so the only way back to that issue is the marker
it was created with. These cases also drive the DISPATCHER, because poll order
is the repository's rather than this transaction's -- an orphan can reach the
stage machine on its own, before anything has attributed it.
"""
from __future__ import annotations

import unittest

from orchestrator.workflow.engine import dispatch as _dispatch
from orchestrator.workflow.stages.decomposition import (
    late_children as _late_children,
)
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateDisposition,
)

from tests.workflow.fixtures import _TEST_SPEC
from tests.workflow.stages.decomposition.late_crash_support import (
    killed_after,
    killed_before,
)
from tests.workflow.stages.decomposition.late_test_support import (
    LATE_ISSUE_NUMBER,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    CHILDREN,
    KEY_CHILDREN,
    KEY_CONSUMERS,
    KEY_EXPECTED_CHILDREN,
    KEY_UMBRELLA,
    LateSplitCase,
)

KEY_AWAITING_HUMAN = "awaiting_human"

KEY_PARK_REASON = "park_reason"

KEY_PARENT_NUMBER = "parent_number"

# A park a child took for itself, after it had been attributed.
_ITS_OWN_PARK = "implementing_timeout"


class ChildBoundaryTest(LateSplitCase, unittest.TestCase):
    """A child is recorded before anything else is done with it."""

    def test_a_death_pre_child_leaves_the_count(self) -> None:
        # What tells a partial split from a finished one, and what says the
        # parent has no implementation of its own to return to.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_before(self.github, "create_child_issue"),
            )

        pinned = self._pinned()
        self.assertEqual(pinned[KEY_EXPECTED_CHILDREN], len(CHILDREN))
        self.assertTrue(pinned[KEY_UMBRELLA])
        self.assertIsNone(pinned.get(KEY_CHILDREN))

    def test_a_death_mid_create_is_visible(self) -> None:
        # The one window the ordering accepts rather than closes: the create
        # returned and the parent never learned of it. The operator sees a
        # count the recorded children do not reach; nothing here silently
        # adopts an issue the parent cannot name.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_after(self.github, "create_child_issue"),
            )

        self.assertEqual(len(self.github.created_child_issues), 1)
        self.assertIsNone(self._pinned().get(KEY_CHILDREN))
        self.assertEqual(
            self._pinned()[KEY_EXPECTED_CHILDREN], len(CHILDREN),
        )

    def test_a_death_between_children_resumes(self) -> None:
        # The first slice is created, recorded, and seeded; the second has not
        # been touched. The resume adopts the first and opens only the second.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(killed=killed_after(_late_children, "_seeded"))
        first = list(self._pinned()[KEY_CHILDREN])
        self.assertEqual(len(first), 1)

        resumed = self._resume()

        self.assertEqual(resumed.disposition, _LateDisposition.SETTLED)
        self.assertEqual(len(self.github.created_child_issues), len(CHILDREN))
        self.assertEqual(self._pinned()[KEY_CHILDREN][:1], first)

    def test_an_orphan_polled_first_is_unparked(self) -> None:
        # Poll order is the repository's, not this transaction's: GitHub sorts
        # by most recently updated, so the child a crash left unrecorded can be
        # dispatched before the write that attributes it -- and a `blocked`
        # issue nobody claims is parked for a human. Adopting it has to take
        # that park back, or the child is activated and then waits for a reply
        # nobody owes it.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_after(self.github, "create_child_issue"),
            )
        orphan = self.github.created_child_issues[0]
        self._dispatch(orphan)
        self.assertTrue(self._child_state(orphan.number)[KEY_AWAITING_HUMAN])

        self._resume()

        seeded = self._child_state(orphan.number)
        self.assertFalse(seeded[KEY_AWAITING_HUMAN])
        self.assertIsNone(seeded[KEY_PARK_REASON])
        self.assertEqual(seeded[KEY_PARENT_NUMBER], LATE_ISSUE_NUMBER)

    def test_a_park_of_its_own_survives_a_reseed(self) -> None:
        # A child that already records a parent has been attributed, so a park
        # on it is something it hit while running -- not this transaction's to
        # take back.
        self._transact()
        adopted = self.github.created_child_issues[0].number
        self.github.seed_state(adopted, **{
            **self._child_state(adopted),
            KEY_AWAITING_HUMAN: True,
            KEY_PARK_REASON: _ITS_OWN_PARK,
        })

        self._resume()

        self.assertTrue(self._child_state(adopted)[KEY_AWAITING_HUMAN])
        self.assertEqual(
            self._child_state(adopted)[KEY_PARK_REASON], _ITS_OWN_PARK,
        )

    def test_a_death_after_the_record_adopts(self) -> None:
        # Recorded first, so the retry reuses the child rather than opening a
        # second issue for the same slice -- and re-seeds it, since the seed
        # is the step that can have been lost.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(killed=killed_after(_late_children, "_recorded"))

        recorded = list(self._pinned()[KEY_CHILDREN])
        self.assertEqual(self._pinned()[KEY_CONSUMERS], recorded)

        resumed = self._resume()

        self.assertEqual(resumed.disposition, _LateDisposition.SETTLED)
        self.assertEqual(
            [child.number for child in self.github.created_child_issues],
            self._pinned()[KEY_CHILDREN],
        )
        self.assertEqual(len(self._pinned()[KEY_CHILDREN]), len(CHILDREN))

    def _dispatch(self, child) -> None:
        """Route one child through the real tick, as a poll would.

        The whole dispatcher rather than the handler it lands on, because what
        is under test is the ORDER a repository polls in: the child reaches the
        stage machine on its own, before anything has attributed it.
        """
        _dispatch._process_issue(self.github, _TEST_SPEC, child)


if __name__ == "__main__":
    unittest.main()
