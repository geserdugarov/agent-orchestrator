# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a guarded split earns once its snapshot and its children are durable."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.workflow.stages.decomposition import (
    late_transaction as _late_transaction,
)
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateDisposition,
    _PlanPrHold,
)
from orchestrator.workflow.state import WorkflowLabel

from tests.workflow.stages.decomposition.late_run_support import (
    adjudicate,
    agent_reply,
)
from tests.workflow.stages.decomposition.late_crash_support import refusing
from tests.workflow.stages.decomposition.late_test_support import (
    CANDIDATE_SHA,
    CYCLE_ID,
    KEYS,
    LATE_ISSUE_NUMBER,
    PLAN_PR_NUMBER,
    QUESTION_REPLY,
    ROOT_ISSUE,
    SPLIT_REPLY,
)
from tests.workflow.stages.decomposition.late_test_support import (
    seeded_late_issue,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    SUPERSESSION_MARKER,
    CHILDREN,
    EVENT_LATE_CLEANUP,
    KEY_CHILDREN,
    KEY_CONSUMERS,
    KEY_LINKS_ANNOUNCED,
    KEY_PR_NUMBER,
    SNAPSHOT_REF,
)
from tests.workflow.stages.decomposition.late_transaction_support import (
    HeldPlanPrSplitCase,
    SnapshotSeed,
    LateSplitCase,
    first_child,
    label_of,
)

RESOURCE_BRANCH = "branch"
RESOURCE_SNAPSHOT = "snapshot_ref"

STATE_RECONCILED = "reconciled"
STATE_FAILED = "failed"
STATE_RETAINED = "retained"

FAILURE_BRANCH_CLEANUP = "branch_cleanup_failed"

UMBRELLA_FRAGMENT = "becomes an umbrella"


class ForwardLinkTest(LateSplitCase, unittest.TestCase):
    """The parent says what it became and where its work went, exactly once."""

    def test_it_names_every_child_and_the_snapshot(self) -> None:
        self._transact()

        posted = self.github.posted_comments[-1][1]
        for child in self.github.created_child_issues:
            self.assertIn(f"#{child.number}", posted)
        self.assertIn(SNAPSHOT_REF, posted)
        self.assertIn(CANDIDATE_SHA, posted)
        self.assertIn(UMBRELLA_FRAGMENT, posted)

    def test_a_stamped_announcement_is_not_repeated(self) -> None:
        # The generation's own receipt rather than the phase, which the
        # owner-read claim every retry passes through rewrites before this
        # owner could read it.
        self._transact()
        said = len(self.github.posted_comments)

        self._resume()

        self.assertEqual(len(self.github.posted_comments), said)
        self.assertTrue(self._pinned()[KEY_LINKS_ANNOUNCED])

    def test_an_earlier_stamp_suppresses_nothing(self) -> None:
        # `decomposed_at` belongs to whichever decomposition last wrote it,
        # and an issue that was decomposed, saw its children resolve, and then
        # implemented an oversized candidate still carries the old one.
        self.github.seed_state(
            self.issue.number,
            **self._pinned(),
            decomposed_at="2026-01-01T00:00:00Z",
        )

        self._transact()

        self.assertEqual(
            len([
                body for _, body in self.github.posted_comments
                if SNAPSHOT_REF in body
            ]),
            1,
        )


class SupersessionTest(HeldPlanPrSplitCase, unittest.TestCase):
    """The held plan PR is told where the work went, and closed."""

    def test_it_restores_the_body_and_closes(self) -> None:
        self._transact(generation=self.generation)

        self.assertEqual(self.plan_pr.state, "closed")
        self.assertIn(
            SUPERSESSION_MARKER,
            self.github.posted_pr_comments[-1][1],
        )

    def test_the_notice_links_forward_to_everything(self) -> None:
        self._transact(generation=self.generation)

        notice = self.github.posted_pr_comments[-1][1]
        self.assertIn(f"#{LATE_ISSUE_NUMBER}", notice)
        self.assertIn(SNAPSHOT_REF, notice)
        self.assertIn(CANDIDATE_SHA, notice)
        for child in self.github.created_child_issues:
            self.assertIn(f"#{child.number}", notice)

    def test_a_settled_pr_is_told_and_left_alone(self) -> None:
        # A human merging or closing the plan PR while the agent ran decided
        # something about that pull request, not about the candidate.
        for merged, state in ((True, "closed"), (False, "closed")):
            with self.subTest(merged=merged):
                self.setUp()
                self.plan_pr.merged = merged
                self.plan_pr.state = state

                outcome = self._transact(generation=self.generation)

                self.assertEqual(outcome.disposition, _LateDisposition.SETTLED)
                self.assertEqual(self.plan_pr.merged, merged)

    def test_a_refused_supersession_activates_nothing(self) -> None:
        # A pull request carrying the superseded work is still open, so no
        # child is let loose beside it.
        self.github.unsupersedable_prs.add(self.plan_pr.number)

        outcome = self._transact(generation=self.generation)

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(
            self._pinned().get(KEYS.park_reason),
            _late_transaction._late_outcome.PARK_SUPERSESSION_FAILED,
        )
        self.assertEqual(
            label_of(self.github, LATE_ISSUE_NUMBER), WorkflowLabel.DECOMPOSING,
        )
        self.assertEqual(first_child(self.github).labels[0].name,
                         WorkflowLabel.BLOCKED)

    def test_an_unreadable_pr_activates_nothing(self) -> None:
        # Both reads it takes -- the hold's own, and the fetch the
        # supersession is made against -- fail closed rather than raising.
        with refusing(self.github, "get_pr"):
            with self.assertLogs(level="ERROR"):
                outcome = self._transact(generation=self.generation)

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(
            label_of(self.github, LATE_ISSUE_NUMBER),
            WorkflowLabel.DECOMPOSING,
        )

    def test_a_pr_unreadable_after_release_parks(self) -> None:
        # The window between the release and the supersession: a lazy pull
        # request raises from a read as readily as from a write, and the
        # children are already live by then.
        released = patch.object(
            _late_transaction._late_hold,
            "_release_plan_pr_hold",
            return_value=_PlanPrHold(generation=self.generation),
        )
        with released:
            with refusing(self.github, "get_pr"):
                with self.assertLogs(level="ERROR"):
                    outcome = self._transact(generation=self.generation)

        self.assertEqual(outcome.disposition, _LateDisposition.PARKED)
        self.assertEqual(len(self.github.created_child_issues), len(CHILDREN))

    def test_a_retry_says_nothing_twice(self) -> None:
        # The children are already durable by then, so the retry is a read and
        # the thread carries one notice.
        self.github.unsupersedable_prs.add(self.plan_pr.number)
        self._transact(generation=self.generation)
        self.github.unsupersedable_prs.clear()

        self._resume()

        self.assertEqual(
            len([
                body for _, body in self.github.posted_pr_comments
                if SUPERSESSION_MARKER in body
            ]),
            1,
        )


class RetirementTest(LateSplitCase, unittest.TestCase):
    """The parent stops being a candidate and starts being an umbrella."""

    def test_it_hands_the_issue_to_umbrella(self) -> None:
        self._transact()

        self.assertEqual(
            label_of(self.github, LATE_ISSUE_NUMBER), WorkflowLabel.UMBRELLA,
        )

    def test_the_measurement_goes_identity_stays(self) -> None:
        # A parent that became an umbrella has no candidate to measure, and a
        # record still answering "oversized" would pin `decomposing` and put
        # the umbrella label back on every tick.
        self._transact()

        pinned = self._pinned()
        self.assertNotIn(KEYS.additions, pinned)
        self.assertNotIn(KEYS.threshold, pinned)
        self.assertEqual(pinned["late_cycle_id"], CYCLE_ID)
        self.assertEqual(pinned["late_root_issue"], ROOT_ISSUE)
        self.assertEqual(pinned[KEYS.candidate_sha], CANDIDATE_SHA)

    def test_the_ledgers_survive_the_retirement(self) -> None:
        # An obligation the remote is owed does not stop being owed because
        # the adjudication that recorded it ended well.
        self._transact()

        pinned = self._pinned()
        self.assertEqual(
            len(pinned[KEY_CONSUMERS]), len(self.github.created_child_issues),
        )
        self.assertEqual(
            self._resources()[(RESOURCE_SNAPSHOT, SNAPSHOT_REF)],
            STATE_RETAINED,
        )

    def test_the_superseded_pull_request_is_dropped(self) -> None:
        # Left in place it would point the merged-PR terminal at a change the
        # umbrella's children are replacing.
        self._transact(pr_number=PLAN_PR_NUMBER)

        self.assertIsNone(self._pinned().get(KEY_PR_NUMBER))


class ActivationTest(LateSplitCase, unittest.TestCase):
    """Children run only once the parent's last write has landed."""

    def test_a_child_with_no_dependencies_is_released(self) -> None:
        self._transact()

        first, second = self.github.created_child_issues
        self.assertEqual(label_of(self.github, first.number), WorkflowLabel.READY)
        self.assertEqual(label_of(self.github, second.number), WorkflowLabel.BLOCKED)

    def test_activation_follows_the_parent_label(self) -> None:
        # A crash between them must not leave a runnable child under a parent
        # still labelled `decomposing`.
        self._transact()

        flipped = [number for number, _ in self.github.label_history]
        self.assertLess(
            flipped.index(LATE_ISSUE_NUMBER),
            flipped.index(first_child(self.github).number),
        )


class BranchCleanupTest(LateSplitCase, unittest.TestCase):
    """The superseded branch is owed, retried, and never in the way."""

    def test_a_deleted_branch_is_recorded_reconciled(self) -> None:
        self._transact()

        self.assertEqual(
            self._resources()[
                (RESOURCE_BRANCH, self.github.deleted_remote_branches[0])
            ],
            STATE_RECONCILED,
        )

    def test_a_failed_delete_holds_no_child_back(self) -> None:
        # Children waiting on a branch deletion would be work stalled on
        # tidiness; what it does gate is the umbrella's terminal completion.
        self.github._pull_state._delete_remote_branch_returns_ok = False

        outcome = self._transact()

        self.assertEqual(outcome.disposition, _LateDisposition.SETTLED)
        self.assertEqual(
            label_of(self.github, first_child(self.github).number),
            WorkflowLabel.READY,
        )
        self.assertIn(
            STATE_FAILED,
            [
                recorded for (kind, _), recorded in self._resources().items()
                if kind == RESOURCE_BRANCH
            ],
        )

    def test_a_failed_delete_reaches_both_sinks(self) -> None:
        self.github._pull_state._delete_remote_branch_returns_ok = False

        self._transact()

        reported = self._events_named(EVENT_LATE_CLEANUP)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["outcome"], STATE_FAILED)
        self.assertIn(
            FAILURE_BRANCH_CLEANUP,
            [
                record["failure"]
                for record in self._events_named("late_failure")
            ],
        )

    def test_a_checkout_that_stays_leaves_it_owed(self) -> None:
        # The first attempt records the WHOLE ordinary cleanup, so a local
        # teardown that did not happen leaves the obligation for the
        # umbrella's terminal to retry rather than reading as settled.
        with self.assertLogs(level="WARNING"):
            outcome = self._transact(snapshot=SnapshotSeed(local_gone=False))

        self.assertEqual(outcome.disposition, _LateDisposition.SETTLED)
        self.assertIn(
            STATE_FAILED,
            [
                recorded for (kind, _), recorded in self._resources().items()
                if kind == RESOURCE_BRANCH
            ],
        )
        self.assertEqual(
            label_of(self.github, first_child(self.github).number),
            WorkflowLabel.READY,
        )

    def test_a_raising_delete_is_recorded(self) -> None:
        with refusing(self.github, "delete_remote_branch"):
            with self.assertLogs(level="ERROR"):
                outcome = self._transact()

        self.assertEqual(outcome.disposition, _LateDisposition.SETTLED)


class NoDuplicateTest(LateSplitCase, unittest.TestCase):
    """A transaction resumed after a park repeats none of its effects."""

    def test_a_resume_creates_nothing_twice(self) -> None:
        self.github.unsupersedable_prs.add(PLAN_PR_NUMBER)
        self._transact()
        created = [child.number for child in self.github.created_child_issues]
        said = len(self.github.posted_comments)

        self._resume()

        self.assertEqual(
            [child.number for child in self.github.created_child_issues],
            created,
        )
        self.assertEqual(len(self.github.posted_comments), said)
        self.assertEqual(self._pinned()[KEY_CHILDREN], created)
        self.assertEqual(
            len(self._events_named("late_snapshot")), len(CHILDREN),
        )


class CoordinatorHandoffTest(unittest.TestCase):
    """A cleared split is the one shape that reaches this transaction."""

    def test_a_guarded_split_runs_the_transaction(self) -> None:
        # The wiring: the owner read is taken, the settlement hands the split
        # on, and the transaction runs from there rather than from a pinned
        # comment that has moved since.
        github, issue = seeded_late_issue()

        outcome, spawn = adjudicate(
            github, issue, agent_reply(SPLIT_REPLY), transact=True,
        )

        spawn.assert_called_once()
        self.assertEqual(outcome.disposition, _LateDisposition.SETTLED)
        self.assertEqual(len(github.created_child_issues), 2)
        self.assertEqual(
            next(
                label.name for label in github.get_issue(issue.number).labels
            ),
            WorkflowLabel.UMBRELLA,
        )

    def test_a_question_reaches_no_transaction(self) -> None:
        # Nothing but a cleared split may create children, so the one verdict
        # that asks a human creates nothing at all.
        github, issue = seeded_late_issue()

        outcome, _spawn = adjudicate(
            github, issue, agent_reply(QUESTION_REPLY), transact=True,
        )

        self.assertEqual(outcome.disposition, _LateDisposition.DECIDED)
        self.assertEqual(github.created_child_issues, [])


if __name__ == "__main__":
    unittest.main()
