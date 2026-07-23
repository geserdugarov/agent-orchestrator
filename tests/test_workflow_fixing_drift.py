# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing drift behavior."""

from __future__ import annotations

import unittest

from tests import fixing_routing_test_support as support

BEHIND_BASE_ISSUE = support.BEHIND_BASE_ISSUE
DIRTY_WORKTREE_ISSUE = support.DIRTY_WORKTREE_ISSUE
DRIFT_PR_HEAD = support.DRIFT_PR_HEAD
FakeGitHubClient = support.FakeGitHubClient
IN_SYNC_ISSUE = support.IN_SYNC_ISSUE
KEY_AWAITING_HUMAN = support.KEY_AWAITING_HUMAN
LABEL_RESOLVING_CONFLICT = support.LABEL_RESOLVING_CONFLICT
PENDING_FIX_AT = support.PENDING_FIX_AT
QUESTION_PARK_ISSUE = support.QUESTION_PARK_ISSUE
REVIEW_TRANSIENT_ISSUE = support.REVIEW_TRANSIENT_ISSUE
SILENT_PARK_ISSUE = support.SILENT_PARK_ISSUE
UNPUSHED_REBASE_ISSUE = support.UNPUSHED_REBASE_ISSUE
_FixingWorktreeDriftFixtureMixin = support._FixingWorktreeDriftFixtureMixin
_TEST_SPEC = support._TEST_SPEC
workflow = support.workflow


class FixingWorktreeDriftRoutingTest(
    _FixingWorktreeDriftFixtureMixin,
    unittest.TestCase,
):
    def test_stuck_push_failed_behind_base_routes(self) -> None:
        # Variant 1: stuck `push_failed` + worktree behind base ->
        # resolving_conflict rebases.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, BEHIND_BASE_ISSUE)
        with self._drift_patches(2):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(BEHIND_BASE_ISSUE))
        self._assert_routed(gh, BEHIND_BASE_ISSUE)
        self.recover.assert_called_once()

    def test_stuck_push_failed_unpushed_rebase_routes(self) -> None:
        # Variant 2: stuck `push_failed` + worktree ON base but local HEAD
        # differs from the stale remote PR head -> resolving_conflict
        # recognises the already-rebased worktree and republishes it.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, UNPUSHED_REBASE_ISSUE)
        with self._drift_patches(0, local_head="079210cabc"):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(UNPUSHED_REBASE_ISSUE))
        self._assert_routed(gh, UNPUSHED_REBASE_ISSUE)

    def test_stuck_push_failed_in_sync_stays_parked(self) -> None:
        # On base AND local HEAD == PR head: drift is not the underlying
        # blocker. The recovery already declared "stuck" -> bail silently
        # so the human can investigate, do not re-post any comment.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, IN_SYNC_ISSUE)
        with self._drift_patches(0, local_head=DRIFT_PR_HEAD):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(IN_SYNC_ISSUE))

        self.assertNotIn((IN_SYNC_ISSUE, LABEL_RESOLVING_CONFLICT), gh.label_history)
        self.assertTrue(gh.pinned_data(IN_SYNC_ISSUE).get(KEY_AWAITING_HUMAN))
        self.post.assert_not_called()

    def test_stuck_push_failed_dirty_stays_parked(self) -> None:
        # A dirty worktree is a park an operator may be inspecting;
        # `resolving_conflict` would reset it to the remote, so leave it.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, DIRTY_WORKTREE_ISSUE)
        with self._drift_patches(5, dirty=("src/x.py",)):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(DIRTY_WORKTREE_ISSUE))

        self.assertNotIn((DIRTY_WORKTREE_ISSUE, LABEL_RESOLVING_CONFLICT), gh.label_history)
        self.assertTrue(gh.pinned_data(DIRTY_WORKTREE_ISSUE).get(KEY_AWAITING_HUMAN))
        self.post.assert_not_called()

    def test_question_park_with_drift_stays_parked(self) -> None:
        # A `park_reason=None` `_on_question` shape could be a real agent
        # question or a "nothing to fix" remark; route neither by inspection.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, QUESTION_PARK_ISSUE, park_reason=None)
        with self._drift_patches(7):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(QUESTION_PARK_ISSUE))

        self.assertNotIn((QUESTION_PARK_ISSUE, LABEL_RESOLVING_CONFLICT), gh.label_history)
        self.assertTrue(gh.pinned_data(QUESTION_PARK_ISSUE).get(KEY_AWAITING_HUMAN))
        self.post.assert_not_called()
        self.recover.assert_not_called()

    def test_review_transient_drift_stays_parked(self) -> None:
        # In_review-route transient parks (`pending_fix_at` set) are
        # deliberately NOT auto-recovered: the round and watermark
        # semantics differ from the validating route.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(
            gh,
            REVIEW_TRANSIENT_ISSUE,
            pending_fix_at=PENDING_FIX_AT,
        )
        with self._drift_patches(4):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(REVIEW_TRANSIENT_ISSUE))

        self.assertNotIn((REVIEW_TRANSIENT_ISSUE, LABEL_RESOLVING_CONFLICT), gh.label_history)
        self.assertTrue(gh.pinned_data(REVIEW_TRANSIENT_ISSUE).get(KEY_AWAITING_HUMAN))
        self.post.assert_not_called()
        self.recover.assert_not_called()

    def test_silent_park_with_drift_stays_parked(self) -> None:
        # `agent_silent` is not in `_VALIDATING_TRANSIENT_PARK_REASONS`
        # (the silent-crash counter is the recovery channel, not drift)
        # so even with `pending_fix_at` unset the issue must stay parked.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, SILENT_PARK_ISSUE, park_reason="agent_silent")
        with self._drift_patches(3):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(SILENT_PARK_ISSUE))

        self.assertNotIn((SILENT_PARK_ISSUE, LABEL_RESOLVING_CONFLICT), gh.label_history)
        self.assertTrue(gh.pinned_data(SILENT_PARK_ISSUE).get(KEY_AWAITING_HUMAN))
        self.post.assert_not_called()
        self.recover.assert_not_called()
