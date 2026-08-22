# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Finding the child an earlier pass created and never recorded.

The one window an ordered register cannot close: `create_child_issue` returned
and the process died before the write, so nothing outside GitHub knows the
number. What leads back to it is the marker the child was created with -- and
what the walk does with a candidate it did not expect is as much of the
contract as finding one at all, because in that window nobody has attributed
the child and a human is free to act on it.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator.workflow.stages.decomposition.late_models import (
    _LateDisposition,
)
from orchestrator.workflow.state import WorkflowLabel

from tests.workflow.stages.decomposition.late_crash_support import (
    killed_after,
    refusing,
)
from tests.workflow.stages.decomposition.late_test_support import (
    KEYS,
    LATE_ISSUE_NUMBER,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    CHILDREN,
    KEY_CHILDREN,
    PARK_CHILDREN_FAILED,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    LateSplitCase,
    sibling_marker,
)

CREATE_CHILD = "create_child_issue"

FIND_ISSUE = "find_issue_carrying"

ERROR = "ERROR"

# A child marker naming a generation this transaction is not running.
_FOREIGN_MARKER = (
    f"<!--orchestrator-late-child:issue={LATE_ISSUE_NUMBER}"
    ":cycle=1:generation=1:index=0-->"
)

# Another issue entirely, adjudicating under the same cycle and generation --
# which is the ordinary case, not a contrived one, since a cycle is minted per
# issue.
OTHER_PARENT = 77

LABEL_BLOCKED = "workflow:blocked"

STATE_CLOSED = "closed"


class _RealShapedOrphan:
    """The orphan the lookup found, in the shape GitHub hands one back in.

    A PyGithub issue carries `state` and nothing called `closed`, so the
    double's flag is the one spelling production never sees. Everything else
    is the live issue, so a walk that read this as open would go on to adopt,
    seed, and start the real thing -- which is what the refusal is for.
    """

    def __init__(self, issue) -> None:
        self._issue = issue
        self.state = STATE_CLOSED

    def __getattr__(self, name: str):
        if name == "closed":
            raise AttributeError(name)
        return getattr(self._issue, name)


class OrphanAdoptionCase(LateSplitCase):
    """A split whose first pass died between the create and the record."""

    def _crashed(self):
        """Create the first slice's child, then die before recording it."""
        with self.assertRaises(KeyboardInterrupt):
            self._transact(killed=killed_after(self.github, CREATE_CHILD))
        return self.github.created_child_issues[0]

    def _recorded(self) -> list:
        return self._pinned().get(KEY_CHILDREN) or []


class OrphanAdoptionTest(OrphanAdoptionCase, unittest.TestCase):
    """A child created into a crash is adopted, never opened twice."""

    def test_an_unrecorded_child_is_adopted(self) -> None:
        # The one window the ordered register cannot close on its own: the
        # create returned and nothing outside GitHub knows the number.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_after(self.github, CREATE_CHILD),
            )
        orphan = self._crashed().number

        resumed = self._resume()

        self.assertEqual(resumed.disposition, _LateDisposition.SETTLED)
        self.assertEqual(
            len(self.github.created_child_issues), len(CHILDREN),
        )
        self.assertEqual(self._recorded()[0], orphan)

    def test_an_unreadable_lookup_creates_nothing(self) -> None:
        # "Could not ask" read as "there is no orphan" is what opens a second
        # issue for a slice that already has one, so the walk parks instead.
        self._crashed()
        created = list(self.github.created_child_issues)

        with refusing(self.github, FIND_ISSUE):
            with self.assertLogs(level=ERROR):
                outcome = self._resume()

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(self.github.created_child_issues, created)
        self.assertEqual(
            self._pinned().get(KEYS.park_reason), PARK_CHILDREN_FAILED,
        )

    def test_a_first_pass_takes_no_lookup(self) -> None:
        # The lookup is a walk over every issue in the repository. A first
        # pass has nothing to find, since no earlier one has run.
        looked = MagicMock()
        with patch.object(self.github, FIND_ISSUE, looked):
            self._transact()

        looked.assert_not_called()

    def test_a_child_a_human_closed_is_left_alone(self) -> None:
        # Reopening it would undo a deliberate act on an issue this
        # orchestrator had not even attributed yet; creating a second one
        # beside it would be worse.
        orphan = self._crashed()
        orphan.closed = True

        with self.assertLogs(level=ERROR):
            outcome = self._resume()

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(len(self.github.created_child_issues), 1)
        self.assertTrue(orphan.closed)

    def test_a_child_a_human_relabelled_is_left_alone(self) -> None:
        orphan = self._crashed()
        self.github.set_workflow_label(
            orphan, WorkflowLabel.REJECTED, guarded=False,
        )

        with self.assertLogs(level=ERROR):
            outcome = self._resume()

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(len(self.github.created_child_issues), 1)

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


class RealShapedOrphanTest(OrphanAdoptionCase, unittest.TestCase):
    """The same refusal, against the shape production is handed.

    Asked for the double's `closed` flag alone -- which no PyGithub issue
    carries -- a closed orphan reads as open, and the split adopts, reseeds,
    and starts an issue a human ended.
    """

    def test_a_real_shaped_closed_child_is_left_alone(self) -> None:
        orphan = self._crashed()
        orphan.closed = True

        with patch.object(
            self.github, FIND_ISSUE, return_value=_RealShapedOrphan(orphan),
        ):
            with self.assertLogs(level=ERROR):
                outcome = self._resume()

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(len(self.github.created_child_issues), 1)
        self.assertEqual(
            self.github.workflow_label(orphan), WorkflowLabel.BLOCKED,
        )


if __name__ == "__main__":
    unittest.main()
