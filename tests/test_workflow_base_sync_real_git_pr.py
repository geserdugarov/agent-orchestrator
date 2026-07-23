# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import base_sync

from tests.fakes import FakeGitHubClient, FakePR, make_issue
from tests.workflow_helpers import (
    LABEL_IN_REVIEW,
    LABEL_RESOLVING_CONFLICT,
    LABEL_VALIDATING,
    STATE_OPEN,
)

from tests.base_sync_real_git_support import (
    _LocalBranchPusher,
    _RefreshBaseRealGitFixture,
)

REPO_SLUG = "acme/widget"
BASE_BRANCH = "main"
PR_BRANCH = "orchestrator/acme__widget/issue-7"
KEY_CONFLICT_ROUND = "conflict_round"
KEY_REVIEW_ROUND = "review_round"
GIT_COMMAND = "git"
ADD_COMMAND = "add"
PUSH_COMMAND = "push"
ORIGIN_REMOTE = "origin"
WORKTREES_DIR_NAME = "worktrees"
WORKTREES_DIR_ATTR = "WORKTREES_DIR"
EXTRA_FILENAME = "extra.txt"
PR_NUMBER = 42


class RefreshPrRealGitTest(_RefreshBaseRealGitFixture, unittest.TestCase):
    def test_clean_base_advance_routes_to_validating(self) -> None:
        self._seed_open_pr(review_round=4)
        self._git(PUSH_COMMAND, ORIGIN_REMOTE, PR_BRANCH, cwd=self._wt)
        self._advance_base(conflicting=False)
        head_before = self._wt_head()
        pusher = _LocalBranchPusher()

        self._refresh_with_push(pusher)

        self._assert_clean_rebase(pusher, head_before)

    def test_push_failure_resets_local_head(self) -> None:
        self._seed_open_pr()
        self._git(PUSH_COMMAND, ORIGIN_REMOTE, PR_BRANCH, cwd=self._wt)
        self._advance_base(conflicting=False)
        head_before = self._wt_head()
        push = MagicMock(return_value=False)

        self._refresh_with_push(push)

        self._assert_push_failure(push, head_before)

    def test_conflicting_base_routes_to_conflict(self) -> None:
        self._seed_open_pr()
        self._advance_base(conflicting=True)
        head_before = self._wt_head()
        push = MagicMock()

        self._refresh_with_push(push)

        self.assertEqual(head_before, self._wt_head())
        self.assertTrue(self._is_clean())
        push.assert_not_called()
        self.assertIn(
            (7, LABEL_RESOLVING_CONFLICT),
            self._gh.label_history,
        )
        self.assertEqual(
            self._gh.pinned_data(7).get(KEY_CONFLICT_ROUND),
            0,
        )

    def _seed_open_pr(self, *, review_round: int | None = None) -> None:
        self._gh = FakeGitHubClient()
        self._gh.add_issue(make_issue(7, label=LABEL_IN_REVIEW))
        state = {
            "pr_number": PR_NUMBER,
            "branch": PR_BRANCH,
        }
        if review_round is not None:
            state["review_round"] = review_round
        self._gh.seed_state(7, **state)
        self._gh.add_pr(
            FakePR(
                number=PR_NUMBER,
                head_branch=PR_BRANCH,
                merged=False,
                state=STATE_OPEN,
            )
        )

    def _refresh_with_push(self, push) -> None:
        with patch.object(base_sync, "_push_branch", side_effect=push):
            self._refresh()

    def _assert_clean_rebase(
        self,
        pusher: _LocalBranchPusher,
        head_before: str,
    ) -> None:
        self.assertNotEqual(head_before, self._wt_head())
        self.assertTrue((self._wt / EXTRA_FILENAME).exists())
        self.assertTrue(self._is_clean())
        self.assertEqual(pusher.branch, PR_BRANCH)
        self.assertEqual(pusher.force_with_lease, head_before)
        self.assertIn((7, LABEL_VALIDATING), self._gh.label_history)
        self.assertNotIn(
            (7, LABEL_RESOLVING_CONFLICT),
            self._gh.label_history,
        )
        state = self._gh.pinned_data(7)
        self.assertEqual(state.get(KEY_REVIEW_ROUND), 0)
        self.assertIsNone(state.get(KEY_CONFLICT_ROUND))

    def _assert_push_failure(self, push, head_before: str) -> None:
        push.assert_called_once()
        self.assertEqual(head_before, self._wt_head())
        self.assertFalse((self._wt / EXTRA_FILENAME).exists())
        self.assertTrue(self._is_clean())
        self.assertEqual(self._gh.label_history, [])
        self.assertEqual(self._gh.posted_pr_comments, [])
        self.assertIsNone(
            self._gh.pinned_data(7).get(KEY_REVIEW_ROUND),
        )


if __name__ == "__main__":
    unittest.main()
