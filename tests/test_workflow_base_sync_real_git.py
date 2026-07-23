# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest


from tests.base_sync_real_git_support import (
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


class RefreshPrePrRealGitTest(_RefreshBaseRealGitFixture, unittest.TestCase):
    def test_clean_advance_rebases_worktree(self) -> None:
        self._advance_base(conflicting=False)
        head_before = self._wt_head()
        self._refresh()
        head_after = self._wt_head()
        self.assertNotEqual(head_before, head_after)
        # The base file landed in the worktree's tree.
        self.assertTrue((self._wt / EXTRA_FILENAME).exists())
        self.assertEqual(
            self._git("log", "-1", "--format=%s", cwd=self._wt).strip(),
            "feat: add feature",
        )
        self.assertTrue(self._is_clean())

    def test_no_op_when_already_up_to_date(self) -> None:
        head_before = self._wt_head()
        self._refresh()
        self.assertEqual(head_before, self._wt_head())
        self.assertTrue(self._is_clean())

    def test_conflict_aborts_leaving_worktree_clean(self) -> None:
        self._advance_base(conflicting=True)
        head_before = self._wt_head()
        self._refresh()
        # HEAD did NOT move (rebase aborted) and worktree is clean again --
        # the conflict surfaces later via the resolving_conflict stage.
        self.assertEqual(head_before, self._wt_head())
        self.assertTrue(self._is_clean())

    def test_dirty_worktree_skips_without_changes(self) -> None:
        self._advance_base(conflicting=False)
        # Plant an uncommitted edit in the worktree -- mirrors a mid-flight
        # agent edit. The base rebase must NOT run.
        (self._wt / "scratch.py").write_text("scratch\n")
        head_before = self._wt_head()
        self._refresh()
        self.assertEqual(head_before, self._wt_head())
        # Untracked file still present, nothing else was added.
        self.assertTrue((self._wt / "scratch.py").exists())
        self.assertFalse((self._wt / EXTRA_FILENAME).exists())
