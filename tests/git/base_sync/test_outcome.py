# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The PR states and counters a refresh must leave exactly as it found them."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from orchestrator.git.base_sync import refresh as _base_refresh
from tests.git.base_sync.refresh_test_support import (
    AFTER_SHA,
    _git_result,
    _patch_base_sync,
    _SyncWorktreeWithBaseFixture,
)

ISSUE = 7

# Worktree HEAD SHAs threaded through the rebase / push flow. The post-rebase
# head comes off the shared fixture, where it is the commit the size gate
# proves the checkout to.
BEFORE_SHA = "be40e5ba" * 5

LABEL_RESOLVING_CONFLICT = "workflow:resolving_conflict"

# Pinned-state field keys read back from `gh.pinned_data(...)`.
KEY_CONFLICT_ROUND = "conflict_round"
KEY_PR_LAST_COMMENT_ID = "pr_last_comment_id"

THREE_BEHIND_STDOUT = "3\n"
UP_TO_DATE_STDOUT = "0\n"

HUMAN_LOGIN = "human"
UNREAD_COMMENT_ID = 500


class PrRefreshOutcomeUnitTest(_SyncWorktreeWithBaseFixture, unittest.TestCase):
    def test_pr_up_to_date_does_not_route(self) -> None:
        # behind = 0 short-circuits: nothing to refresh, no detour.
        self._seed_pr_issue()
        self._add_pr()
        git_mock = MagicMock(return_value=_git_result(stdout=UP_TO_DATE_STDOUT))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            git=git_mock,
        ):
            _base_refresh._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_route_keeps_existing_conflict_round(self) -> None:
        # On a conflict-driven re-entry from a previous resolving_conflict
        # round, the cap counter must NOT reset to 0 -- mirrors
        # `_handle_in_review`'s "set when absent" semantics so a
        # perpetually-stuck PR can't ping-pong forever. The clean-rebase
        # path no longer touches `conflict_round`; this test exercises
        # the conflicted-files path where the counter is still seeded.
        self._seed_pr_issue(conflict_round=2)
        self._add_pr()
        merge = MagicMock(return_value=(False, ["a.py"]))
        head_sha = MagicMock(return_value=BEFORE_SHA)
        git_mock = MagicMock(return_value=_git_result(stdout="1\n"))
        hardened = MagicMock(return_value=_git_result())
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            rebase=merge,
            head_sha=head_sha,
            git=git_mock,
            hardened=hardened,
        ):
            _base_refresh._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)
        state = self.gh.pinned_data(ISSUE)
        # Existing counter (2) preserved, not reset to 0.
        self.assertEqual(state.get(KEY_CONFLICT_ROUND), 2)
        # The conflict path still flips to resolving_conflict.
        self.assertIn((ISSUE, LABEL_RESOLVING_CONFLICT), self.gh.label_history)

    def test_pr_route_skips_merged_pr(self) -> None:
        # Regression: a just-merged PR advances `origin/<base>`, so the
        # still-in_review worktree pointed at the now-stale branch is
        # naturally behind. Without the PR-state gate the refresh would
        # post an "auto-resolution" notice and relabel the issue to
        # `resolving_conflict` on a PR the next handler call would
        # finalize to `done`. Leaving the label alone lets the existing
        # in_review terminal handler (or the closed-issue sweep variant)
        # do its job.
        self._seed_pr_issue()
        self._add_pr(merged=True, state="closed")
        git_mock = MagicMock(return_value=_git_result(stdout=THREE_BEHIND_STDOUT))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            git=git_mock,
        ):
            _base_refresh._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_skips_closed_unmerged_pr(self) -> None:
        # Same regression for the rejected terminal: a closed-without-merge
        # PR that happens to be behind base must not be relabeled to
        # `resolving_conflict`. The handler will finalize to `rejected`.
        self._seed_pr_issue()
        self._add_pr(merged=False, state="closed")
        git_mock = MagicMock(return_value=_git_result(stdout=THREE_BEHIND_STDOUT))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            git=git_mock,
        ):
            _base_refresh._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_skips_when_get_pr_fails(self) -> None:
        # Defensive: if PR state cannot be determined this tick, leave the
        # label alone -- the handler can retry from a stable label rather
        # than racing a half-known state.
        self._seed_pr_issue(with_pr=False)
        # No PR added -- get_pr will raise KeyError on the FakeGitHubClient.
        git_mock = MagicMock(return_value=_git_result(stdout=THREE_BEHIND_STDOUT))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            git=git_mock,
        ):
            _base_refresh._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_route_keeps_review_watermark(self) -> None:
        # Regression: the refresh-time flow runs BEFORE any handler scans
        # comments. Bumping `pr_last_comment_id` past `latest_comment_id`
        # would silently mark unread human "do not merge" / fix-request
        # comments as consumed; the next `_handle_in_review` scan would
        # then skip them and the in_review HITL ready-ping could
        # advertise the PR as ready for human merge over the human
        # signal. The watermark must be left alone on both branches of
        # the new flow (clean rebase + conflicted rebase) -- the next
        # in_review scan will pick the human comments up correctly, and
        # the orchestrator's own PR notice is filtered via
        # `orchestrator_comment_ids` so it does not replay either.
        self._seed_pr_issue(pr_last_comment_id=100)
        self._add_pr()
        # An UNREAD human comment landed AFTER the current watermark of 100.
        # If we bump the watermark to `latest_comment_id` (max id seen, which
        # would include this human comment), it gets silently consumed.
        self._add_comment(UNREAD_COMMENT_ID, "do not merge yet", HUMAN_LOGIN)
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        head_sha = MagicMock(
            side_effect=[BEFORE_SHA, AFTER_SHA, AFTER_SHA],
        )
        git_mock = MagicMock(return_value=_git_result(stdout="1\n"))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            rebase=merge,
            push=push,
            head_sha=head_sha,
            git=git_mock,
        ):
            _base_refresh._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)
        state = self.gh.pinned_data(ISSUE)
        # Watermark stayed at 100 -- the unread human comment at id=500 is
        # still ahead of it and the next in_review scan will pick it up.
        self.assertEqual(state.get(KEY_PR_LAST_COMMENT_ID), 100)

    def test_pr_route_skips_when_awaiting_human(self) -> None:
        # Regression: a parked PR (`awaiting_human=True`) must not be
        # detoured. `_handle_resolving_conflict`'s awaiting-human branch
        # returns early without rebasing unless a new human comment arrives,
        # so relabeling here would silently hide the existing park behind a
        # `resolving_conflict` label without making any progress -- including
        # the documented `in_review` unmergeable park path. Leaving the
        # park intact preserves its visibility and the human-driven recovery
        # path the park already invited.
        self._seed_pr_issue(
            awaiting_human=True,
            park_reason="unmergeable",
        )
        git_mock = MagicMock(return_value=_git_result(stdout=THREE_BEHIND_STDOUT))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            git=git_mock,
        ):
            _base_refresh._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)
        # No relabel: park left intact.
        self.assertEqual(self.gh.label_history, [])
        # No PR notice posted (would have been duplicate noise on a parked
        # issue that already has an HITL ping).
        self.assertEqual(self.gh.posted_pr_comments, [])


if __name__ == "__main__":
    unittest.main()
