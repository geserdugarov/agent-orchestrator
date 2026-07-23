# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import workflow

from tests.workflow_helpers import (
    _ResolvingConflictMixin,
    _agent,
)

CONFLICT_ISSUE = 200


class ResolvingConflictStaleDivergedTest(unittest.TestCase, _ResolvingConflictMixin):
    """Drive `_handle_resolving_conflict` through the conservative
    stale / diverged worktree parks: a worktree behind or diverged from
    `origin/<branch>` must refuse to force-push and park awaiting human.
    """

    def test_stale_worktree_parks_awaiting_human(self) -> None:
        # Worktree behind `origin/<branch>` (someone pushed to the PR
        # branch out-of-band). Force-pushing the local state would
        # clobber the real PR head; refuse and park.
        gh, issue, _ = self._seed()

        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run_resolving_conflict(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(0, 2),
            )
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(CONFLICT_ISSUE).get("awaiting_human"))
        self.assertNotIn((CONFLICT_ISSUE, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("stale or diverged", last_comment)

    def test_diverged_worktree_parks_awaiting_human(self) -> None:
        # Both ahead and behind: histories diverged. Cannot safely push
        # without rewriting remote history that may have value.
        gh, issue, _ = self._seed()

        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run_resolving_conflict(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(1, 1),
            )
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        state = gh.pinned_data(CONFLICT_ISSUE)
        self.assertTrue(state.get("awaiting_human"))
        self.assertNotIn((CONFLICT_ISSUE, "validating"), gh.label_history)


if __name__ == "__main__":
    unittest.main()
