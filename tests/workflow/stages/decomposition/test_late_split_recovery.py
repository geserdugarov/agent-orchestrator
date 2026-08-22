# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a human saying something after a parked split transaction earns.

The coordinator settles fresh edits and guidance BEFORE it replays a recorded
verdict, so every transaction park is a moment at which the humans can move the
requirements out from under a split that has already had irreversible effects.
These cases drive the whole coordinator -- the content settlement, the recorded
short circuit, the owner read, and the transaction behind them -- because what
is under test is the order those run in.

Two rules come out of it. A generation that has already created children may
not be revised into a new one: the children exist, carry an ancestry naming the
adjudication that made them, and are the consumers a snapshot is retained for,
so a second manifest over the top of them strands every one. A generation that
advances at all carries none of the previous one's positional register or its
link receipt -- the two values that would otherwise have a new manifest adopt
an old child by index and swallow the announcement it owes.
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from orchestrator.git.snapshots.refs import SnapshotOutcome
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateDisposition,
)

from tests.workflow.stages.decomposition.late_content_support import (
    KEY_GENERATION,
)
from tests.workflow.stages.decomposition.late_revision_support import (
    RevisionCase,
)
from tests.workflow.stages.decomposition.late_run_support import (
    SnapshotSeed,
    adjudicate,
    agent_reply,
)
from tests.workflow.stages.decomposition.late_test_support import (
    KEYS,
    LATE_ISSUE_NUMBER,
    SPLIT_REPLY,
    seeded_late_issue,
)

PARK_SNAPSHOT_FAILED = "late_snapshot_failed"

PARK_REVISION_UNANSWERED = "late_revision_unanswered"

# A child an earlier pass of this split created and recorded.
CREATED_CHILD = 4242

# The remote refusing to preserve the candidate, which parks the transaction
# before a single child exists.
NO_SNAPSHOT = SnapshotSeed(create=SnapshotOutcome.REFUSED)


def rewrite_generation(github, issue, **fields) -> None:
    """Put this issue's generation back with these fields replaced."""
    state = github.read_pinned_state(issue)
    _late_state.write_late_generation(
        state, replace(_late_state.read_late_generation(state), **fields),
    )
    github.seed_state(issue.number, **state.data)


def generation_of(github, issue):
    """The generation this issue's pinned comment currently records."""
    return _late_state.read_late_generation(github.read_pinned_state(issue))


class ParkedTransactionTest(unittest.TestCase):
    """A split that created nothing leaves nothing of itself behind."""

    def setUp(self) -> None:
        seeded = seeded_late_issue()
        self.github = seeded[0]
        self.issue = seeded[1]

    def test_a_snapshot_park_leaves_no_register(self) -> None:
        parked = self._split(snapshot=NO_SNAPSHOT)

        self.assertEqual(parked.disposition, _LateDisposition.PARKED)
        self.assertEqual(
            self.github.pinned_data(LATE_ISSUE_NUMBER)[KEYS.park_reason],
            PARK_SNAPSHOT_FAILED,
        )
        recorded = generation_of(self.github, self.issue)
        self.assertEqual(recorded.split_children, ())
        self.assertFalse(recorded.links_announced)

    def test_a_quiet_replay_completes_the_same_split(self) -> None:
        # Nothing the humans did, so the recorded verdict is what the next
        # tick reuses -- with no second agent run.
        self._split(snapshot=NO_SNAPSHOT)

        settled, spawn = adjudicate(self.github, self.issue, transact=True)

        spawn.assert_not_called()
        self.assertEqual(settled.disposition, _LateDisposition.SETTLED)
        self.assertEqual(len(self.github.created_child_issues), 2)

    def _split(self, **run_fields):
        """Adjudicate once, running the real transaction."""
        outcome, _spawn = adjudicate(
            self.github, self.issue, agent_reply(SPLIT_REPLY),
            transact=True, **run_fields,
        )
        return outcome


class RevisedAfterChildrenTest(RevisionCase, unittest.TestCase):
    """A split that created children may not be revised out from under them."""

    def setUp(self) -> None:
        self._seed_drifted()
        rewrite_generation(
            self.github, self.issue, split_children=(CREATED_CHILD,),
        )

    def test_guidance_after_a_created_child_parks(self) -> None:
        before = self._pinned()[KEY_GENERATION]

        parked, spawn = self._revise()

        spawn.assert_not_called()
        self.assertEqual(parked.disposition, _LateDisposition.PARKED)
        self.assertEqual(
            self._pinned()[KEYS.park_reason], PARK_REVISION_UNANSWERED,
        )
        self.assertEqual(self._pinned()[KEY_GENERATION], before)

    def test_the_park_names_what_it_will_not_strand(self) -> None:
        self._revise()

        self.assertIn(
            f"#{CREATED_CHILD}", self.github.posted_comments[-1][1],
        )

    def test_the_register_is_left_exactly_as_it_stood(self) -> None:
        self._revise()

        self.assertEqual(
            generation_of(self.github, self.issue).split_children,
            (CREATED_CHILD,),
        )


class RevisedBeforeChildrenTest(RevisionCase, unittest.TestCase):
    """A generation that advances carries none of the last one's receipts.

    The refusal above is what makes this unreachable in practice -- a link
    receipt is only ever written once children exist, and children stop a
    revision. It is asserted anyway because the invariant is the field's own:
    a receipt belongs to the generation that wrote it, and one carried forward
    would swallow the announcement the next split owes.
    """

    def test_a_link_receipt_does_not_survive(self) -> None:
        self._seed_drifted()
        rewrite_generation(self.github, self.issue, links_announced=True)
        before = self._pinned()[KEY_GENERATION]

        revised, _spawn = self._revise()

        self.assertEqual(revised.disposition, _LateDisposition.REVISED)
        self.assertGreater(self._pinned()[KEY_GENERATION], before)
        self.assertFalse(
            generation_of(self.github, self.issue).links_announced,
        )


if __name__ == "__main__":
    unittest.main()
