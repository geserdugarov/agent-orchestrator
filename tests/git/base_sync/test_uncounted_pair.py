# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The pair one auto rebase froze for the size gate and never counted.

The freeze is durable and the count behind it is not, so a tick lost between
them leaves a generation naming both commits with no number on it -- and the
anchor the rebase pinned before git ran standing beside it. Two owners could
answer that, and only one of them can.

The refresh cannot. Its own freeze sets ONE record aside for its anchor, the
approval leased to it, and a reading it froze and never answered is
deliberately not that record: it holds the branch still on every tick, so the
recovery the anchor exists for never reaches the comment. So the count is the
reconciliation's, and a reconciliation that stood down for the anchor would be
waiting for a tick that cannot come.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator.git.base_sync import frozen as _frozen
from orchestrator.git.measurement import additions as _additions
from orchestrator.workflow.stages.implementing import (
    late_reconcile as _reconcile,
)
from orchestrator.workflow.state import WorkflowLabel
from tests.git.base_sync.exemption_test_support import _CleanRebaseCase
from tests.git.base_sync.refresh_test_support import (
    AFTER_SHA,
    BEFORE_SHA,
    ISSUE,
    KEY_PARK_REASON,
    LABEL_IN_REVIEW,
)

# What a process that never came back looks like from inside the tick, and the
# seam it is stopped at: the count the gate takes between the pair the write
# before it made durable.
DIED = "the process died before the tick returned"
COUNT_ADDED_LINES = "_count_added_lines"

# The pair one frozen reading records, and the number the count writes beside
# it -- absent for exactly the window between the two.
KEY_CANDIDATE_SHA = "late_candidate_sha"
KEY_ADDITIONS = "late_additions"

KEY_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"

# What a reading this tick could not take is parked as.
PARK_MEASUREMENT_FAILED = "late_measurement_failed"


class UncountedPairTest(_CleanRebaseCase, unittest.TestCase):
    """A rebase measured by the ordinary gate, stopped between the two."""

    def test_the_count_is_not_deferred_to_the_refresh(self) -> None:
        # The premise is the stalemate: a pair with no number, the anchor
        # still pinned, and the refresh held still by that very record. The
        # answer is that this owner takes it rather than waiting -- which on
        # a host holding the checkout is the count and the push it earns, and
        # here, where the doubles have no checkout to measure in, is that
        # road's own fail-closed refusal. Either is an answer; deferring is
        # not, and leaves every later tick to repeat the same stalemate.
        self._crashes_before_the_count()
        durable = self._durable()

        held = self._reconciles()

        self.assertEqual(durable.get(KEY_CANDIDATE_SHA), AFTER_SHA)
        self.assertIsNone(durable.get(KEY_ADDITIONS))
        self.assertEqual(durable.get(KEY_PENDING_PUSH_SHA), BEFORE_SHA)
        self.assertTrue(_frozen._held_records(durable))
        self.assertTrue(held)
        self.assertEqual(
            self.gh.pinned_data(ISSUE).get(KEY_PARK_REASON),
            PARK_MEASUREMENT_FAILED,
        )

    def _crashes_before_the_count(self) -> None:
        """Freeze the pair the gate measures over, and die before counting."""
        with patch.object(
            _additions, COUNT_ADDED_LINES,
            MagicMock(side_effect=RuntimeError(DIED)),
        ), self.assertRaises(RuntimeError):
            self._rebases()

    def _reconciles(self) -> bool:
        """Run the reconciliation every handler is dispatched behind."""
        issue = self.gh._issues[ISSUE]
        return _reconcile._reconciles_published_work(
            self.gh, self.spec, issue, WorkflowLabel(LABEL_IN_REVIEW),
            self.gh.read_pinned_state(issue),
        )


if __name__ == "__main__":
    unittest.main()
