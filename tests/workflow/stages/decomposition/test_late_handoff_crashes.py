# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a split leaves behind when it dies between an effect and its record.

The back half of the transaction: the supersession the held plan PR is closed
under, the write that hands the parent to `umbrella`, the activation behind it,
and the branch it still owes. Each case kills the process at one seam and runs
the transaction again from what the pinned comment holds -- exactly what the
next eligible tick does, since the verdict is already recorded and the retry
costs a read rather than an agent.
"""
from __future__ import annotations

import unittest

from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.stages.decomposition import (
    late_relabel as _late_relabel,
    late_transaction as _late_transaction,
)
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateDisposition,
)
from orchestrator.workflow.state import WorkflowLabel

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
    SUPERSESSION_MARKER,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    HeldPlanPrSplitCase,
    LateSplitCase,
    label_of,
)

RESOURCE_BRANCH = "branch"

STATE_PENDING = "pending"

STATE_RECONCILED = "reconciled"

PR_OPEN = "open"

PR_CLOSED = "closed"

# A manifest whose slices depend on nothing, so activation has two flips to
# make and a death can land between them.
_INDEPENDENT = (
    {"title": "A", "body": "the first slice"},
    {"title": "B", "body": "the second slice"},
)


class SupersessionBoundaryTest(HeldPlanPrSplitCase, unittest.TestCase):
    """The pull request's own thread is what stops a repeated notice."""

    def test_a_death_post_notice_says_it_once(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                generation=self.generation,
                killed=killed_after(self.github, "supersede_pr"),
            )

        self._resume()

        self.assertEqual(self._notices(), 1)
        self.assertEqual(self.plan_pr.state, PR_CLOSED)

    def test_a_death_between_notice_and_close_closes(self) -> None:
        # The notice landed and the close did not, which the thread rather
        # than a receipt is what recognizes: the resume says nothing twice and
        # finishes the half that was left.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                generation=self.generation,
                killed=killed_after(self.github, "pr_comment"),
            )
        self.assertEqual(self.plan_pr.state, PR_OPEN)

        self._resume()

        self.assertEqual(self._notices(), 1)
        self.assertEqual(self.plan_pr.state, PR_CLOSED)

    def test_a_reopened_pr_is_closed_again(self) -> None:
        # The ledger records what an EARLIER pass did, and a pull request is
        # not a thing that stays where it was put: skipping on the strength of
        # that entry would activate children beside a reopened change still
        # carrying the superseded work.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                generation=self.generation,
                killed=killed_before(_late_transaction, "_handed_to_children"),
            )
        self.assertEqual(self.plan_pr.state, PR_CLOSED)
        self.plan_pr.state = PR_OPEN

        resumed = self._resume()

        self.assertEqual(resumed.disposition, _LateDisposition.SETTLED)
        self.assertEqual(self.plan_pr.state, PR_CLOSED)
        self.assertEqual(self._notices(), 1)

    def test_a_reopened_pr_refusing_holds_activation(self) -> None:
        # And where it cannot be closed again, nothing is handed on: the
        # parent stays on `decomposing` for the next tick to ask.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                generation=self.generation,
                killed=killed_before(_late_transaction, "_handed_to_children"),
            )
        self.plan_pr.state = PR_OPEN
        self.github.unsupersedable_prs.add(self.plan_pr.number)

        resumed = self._resume()

        self.assertEqual(resumed.disposition, _LateDisposition.PARKED)
        self.assertEqual(self.plan_pr.state, PR_OPEN)
        self.assertEqual(
            label_of(self.github, LATE_ISSUE_NUMBER),
            WorkflowLabel.DECOMPOSING,
        )

    def _notices(self) -> int:
        """How many supersession notices this generation's marker is on."""
        return len([
            body for _, body in self.github.posted_pr_comments
            if SUPERSESSION_MARKER in body
        ])


class HandoffBoundaryTest(LateSplitCase, unittest.TestCase):
    """The parent is handed on before a child can run, and cleaned up after."""

    def test_a_death_between_label_and_write_repairs(self) -> None:
        # The window the relabel guard exists for: an `umbrella` label over a
        # generation that still reads oversized. The next tick puts the label
        # back and re-runs the transaction, which adopts everything durable.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_after(self.github, "set_workflow_label"),
            )
        created = [child.number for child in self.github.created_child_issues]
        self.assertTrue(_late_relabel._adjudication_is_live(
            _late_state.read_late_generation(
                self.github.read_pinned_state(self.issue),
            ),
        ))

        resumed = self._resume()

        self.assertEqual(resumed.disposition, _LateDisposition.SETTLED)
        self.assertEqual(
            [child.number for child in self.github.created_child_issues],
            created,
        )
        self.assertEqual(
            label_of(self.github, LATE_ISSUE_NUMBER), WorkflowLabel.UMBRELLA,
        )

    def test_a_death_mid_activation_leaves_the_rest(self) -> None:
        # Activation is best-effort by design, and the umbrella's own walk is
        # the retry -- so a half-done pass leaves the parent handed on and the
        # child it did not reach still `blocked`, which that walk releases.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                children=_INDEPENDENT,
                killed=killed_after(
                    self.github, "set_workflow_label", after=2,
                ),
            )

        first, second = self.github.created_child_issues
        self.assertEqual(
            label_of(self.github, LATE_ISSUE_NUMBER), WorkflowLabel.UMBRELLA,
        )
        self.assertEqual(label_of(self.github, first.number),
                         WorkflowLabel.READY)
        self.assertEqual(label_of(self.github, second.number),
                         WorkflowLabel.BLOCKED)

    def test_a_death_pre_activation_is_umbrella(self) -> None:
        # A crash here cannot leave a runnable child under a parent still
        # labelled `decomposing`; the umbrella's own walk is the retry.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_before(_late_transaction._split,
                                     "_activate_initial_split_children"),
            )

        self.assertEqual(
            label_of(self.github, LATE_ISSUE_NUMBER), WorkflowLabel.UMBRELLA,
        )
        self.assertEqual(len(self._pinned()[KEY_CHILDREN]), len(CHILDREN))
        self.assertEqual(
            label_of(self.github, self.github.created_child_issues[0].number),
            WorkflowLabel.BLOCKED,
        )

    def test_a_death_pre_cleanup_leaves_it_owed(self) -> None:
        # The obligation is durable before the delete is attempted, so a
        # reclamation has something to retry rather than a gap.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_before(self.github, "delete_remote_branch"),
            )

        owed = [
            recorded for (kind, _), recorded in self._resources().items()
            if kind == RESOURCE_BRANCH
        ]
        self.assertEqual(owed, [STATE_PENDING])

    def test_a_death_post_delete_reconciles(self) -> None:
        # The delete landed and the write that recorded it did not. Absent is
        # success, so the resume asks once and settles the same entry.
        with self.assertRaises(KeyboardInterrupt):
            self._transact(
                killed=killed_after(self.github, "delete_remote_branch"),
            )
        self.assertEqual(
            [
                recorded for (kind, _), recorded in self._resources().items()
                if kind == RESOURCE_BRANCH
            ],
            [STATE_PENDING],
        )

        self._resume()

        owed = [
            recorded for (kind, _), recorded in self._resources().items()
            if kind == RESOURCE_BRANCH
        ]
        self.assertEqual(owed, [STATE_RECONCILED])
        self.assertEqual(
            len(self.github.deleted_remote_branches), 2,
        )


if __name__ == "__main__":
    unittest.main()
