# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing conflict behavior."""

from __future__ import annotations

import unittest

from tests import fixing_routing_test_support as support

CONFLICT_FIXTURE_ISSUE = support.CONFLICT_FIXTURE_ISSUE
LABEL_RESOLVING_CONFLICT = support.LABEL_RESOLVING_CONFLICT
LABEL_VALIDATING = support.LABEL_VALIDATING
MagicMock = support.MagicMock
_FixingConflictFixtureMixin = support._FixingConflictFixtureMixin
base_sync = support.base_sync
patch = support.patch
workflow = support.workflow


class FixingConflictDetourTest(
    _FixingConflictFixtureMixin,
    unittest.TestCase,
):
    def test_clean_rebase_keeps_pending_feedback(self) -> None:
        # A clean refresh-time rebase now routes the `fixing` issue to
        # `validating` (no longer to `resolving_conflict`). Either way
        # the pending-fix bookmarks and in_review watermarks must
        # survive the relabel.
        self._seed_fixing_with_pending_feedback()
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        head_sha = MagicMock(side_effect=["before", "after"])
        git_mock = patch.object(
            base_sync,
            "_git",
            return_value=self._git_result(stdout="3\n"),
        )
        with (
            patch.object(base_sync, "_worktree_dirty_files", return_value=[]),
            patch.object(base_sync, "_rebase_base_into_worktree", merge),
            patch.object(base_sync, "_push_branch", push),
            patch.object(base_sync, "_head_sha", head_sha),
            git_mock,
        ):
            workflow._sync_worktree_with_base(
                self.gh,
                self.spec,
                self.wt,
                CONFLICT_FIXTURE_ISSUE,
            )

        # Clean rebase routed `fixing` straight to `validating`.
        self.assertIn((CONFLICT_FIXTURE_ISSUE, LABEL_VALIDATING), self.gh.label_history)
        self.assertNotIn((CONFLICT_FIXTURE_ISSUE, LABEL_RESOLVING_CONFLICT), self.gh.label_history)
        self._assert_pending_feedback_intact()

    def test_conflict_rebase_keeps_pending_feedback(self) -> None:
        # A conflicting refresh-time rebase still routes to
        # `resolving_conflict` so the handler can drive the dev agent.
        # The pending-fix bookmarks and watermarks must survive that
        # relabel too.
        self._seed_fixing_with_pending_feedback()
        merge = MagicMock(return_value=(False, ["src/feature.py"]))
        push = MagicMock()
        head_sha = MagicMock(return_value="before")
        hardened = MagicMock(return_value=self._git_result())
        git_mock = patch.object(
            base_sync,
            "_git",
            return_value=self._git_result(stdout="3\n"),
        )
        with (
            patch.object(base_sync, "_worktree_dirty_files", return_value=[]),
            patch.object(base_sync, "_rebase_base_into_worktree", merge),
            patch.object(base_sync, "_push_branch", push),
            patch.object(base_sync, "_head_sha", head_sha),
            patch.object(base_sync, "_git_hardened", hardened),
            git_mock,
        ):
            workflow._sync_worktree_with_base(
                self.gh,
                self.spec,
                self.wt,
                CONFLICT_FIXTURE_ISSUE,
            )

        self.assertIn((CONFLICT_FIXTURE_ISSUE, LABEL_RESOLVING_CONFLICT), self.gh.label_history)
        self.assertNotIn((CONFLICT_FIXTURE_ISSUE, LABEL_VALIDATING), self.gh.label_history)
        push.assert_not_called()
        self._assert_pending_feedback_intact()
