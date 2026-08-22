# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a child's own state does while the supersession is parked.

The park between the children and the activation can stand for as long as a
human takes to settle a pull request, so by the time the transaction resumes a
child's state is no longer its to assume. The transition guard only warns by
default, so nothing else would stop a write that put a terminal child back to
`ready`.
"""
from __future__ import annotations

import unittest

from orchestrator.workflow.state import WorkflowLabel

from tests.workflow.stages.decomposition.late_test_support import (
    PLAN_PR_NUMBER,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    HeldPlanPrSplitCase,
    first_child,
    label_of,
)


class SupersessionRaceTest(HeldPlanPrSplitCase, unittest.TestCase):
    """What a child's own state does while the supersession is parked.

    The park can stand for as long as a human takes to settle a pull request,
    so by the time activation runs a child's state is no longer this
    transaction's to assume -- and the transition guard only warns by default,
    so nothing else would stop a write that put a terminal child back.
    """

    def setUp(self) -> None:
        super().setUp()
        self.github.unsupersedable_prs.add(PLAN_PR_NUMBER)
        self._transact(generation=self.generation)
        self.github.unsupersedable_prs.clear()

    def test_a_child_that_ended_meanwhile_is_left(self) -> None:
        ended = first_child(self.github)
        self.github.set_workflow_label(
            ended, WorkflowLabel.REJECTED, guarded=False,
        )

        self._resume()

        self.assertEqual(
            label_of(self.github, ended.number), WorkflowLabel.REJECTED,
        )

    def test_a_child_still_blocked_is_released(self) -> None:
        # The other half of the same read: the walk moves the ones that are
        # still where the split left them.
        self._resume()

        self.assertEqual(
            label_of(self.github, first_child(self.github).number),
            WorkflowLabel.READY,
        )

    def test_a_child_the_manifest_holds_stays_blocked(self) -> None:
        self._resume()

        held = self.github.created_child_issues[1]
        self.assertEqual(
            label_of(self.github, held.number), WorkflowLabel.BLOCKED,
        )


if __name__ == "__main__":
    unittest.main()
