# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from orchestrator import workflow
from orchestrator.github import BACKLOG_LABEL, PAUSED_LABEL

from tests.fakes import (
    FakeLabel,
    make_issue,
)

# --- Shared base-sync fixture literals -----------------------------------
# One worktree per issue drives every scenario here: issue #7 with an open
# PR #42 on the canonical head branch of the `acme/widget` target repo.
from tests.base_sync_test_support import (
    _SyncWorktreeWithBaseFixture,
    _git_result,
    _patch_base_sync,
)

ISSUE = 7
PR_NUMBER = 42
SLUG = "acme/widget"
BASE_BRANCH = "main"
PR_BRANCH = "orchestrator/acme__widget/issue-7"

# Multi-remote spec exercised by the per-spec authed-fetch regression.
PRIVATE_SLUG = "acme/widget-private"
PRIVATE_BASE_BRANCH = "cache-main"
PRIVATE_REMOTE = "private"

# Worktree HEAD SHAs threaded through the rebase / push / recovery flows.
BEFORE_SHA = "before-sha"
AFTER_SHA = "after-sha"
REBASED_SHA = "rebased-sha"
# Remote PR head planted so the conflict-round event can assert its `sha`.
CONFLICT_PR_HEAD_SHA = "cafef00dcafef00d"

# Workflow labels the refresh routes between.
LABEL_IN_REVIEW = "in_review"
LABEL_VALIDATING = "validating"
LABEL_RESOLVING_CONFLICT = "resolving_conflict"
LABEL_DOCUMENTING = "documenting"
LABEL_IMPLEMENTING = "implementing"

# Audit event names emitted by the base-sync flow.
EVENT_BASE_REBASED = "base_rebased"
EVENT_CONFLICT_ROUND = "conflict_round"

# Awaiting-human park reasons the auto-rebase flow writes.
PARK_PUSH_FAILED = "auto_base_rebase_push_failed"
PARK_DIRTY = "auto_base_rebase_dirty"
PARK_FAILED = "auto_base_rebase_failed"

# Pinned-state field keys read back from `gh.pinned_data(...)`.
KEY_AWAITING_HUMAN = "awaiting_human"
KEY_PARK_REASON = "park_reason"
KEY_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"
KEY_REVIEW_ROUND = "review_round"
KEY_CONFLICT_ROUND = "conflict_round"
KEY_LAST_ACTION_COMMENT_ID = "last_action_comment_id"
KEY_PR_LAST_COMMENT_ID = "pr_last_comment_id"

# Git output, command, and event fields shared by the scenario assertions.
THREE_BEHIND_STDOUT = "3\n"
TWO_BEHIND_STDOUT = "2\n"
UP_TO_DATE_STDOUT = "0\n"
REBASE_COMMAND = "rebase"
ABORT_FLAG = "--abort"
RESET_COMMAND = "reset"
HARD_RESET_FLAG = "--hard"
FORCE_WITH_LEASE_KWARG = "force_with_lease"
EVENT_FIELD = "event"
SHA_FIELD = "sha"
METHOD_FIELD = "method"

# Stable identities and values used across park and recovery scenarios.
HUMAN_LOGIN = "human"
PARK_WATERMARK_COMMENT_ID = 99
RETRY_COMMENT_ID = 200
OUTSIDER_COMMENT_ID = 201
UNREAD_COMMENT_ID = 500
GIT_FAILURE_EXIT_CODE = 128
MISSING_ISSUE_NUMBER = 9999
NEW_REBASED_SHA = "new-rebased-sha"


class PrRefreshRoutingGuardUnitTest(
    _SyncWorktreeWithBaseFixture,
    unittest.TestCase,
):
    def test_pr_stale_anchor_cleared_when_pr_terminal(self) -> None:
        # Same cleanup contract for the terminal-PR early return: a
        # merged / closed PR makes the recovery target meaningless, so
        # the anchor must not survive into a possibly re-opened future.
        self._seed_pr_issue(
            pending_auto_base_rebase_push_sha="stale-anchor",
        )
        # Merged PR -- terminal.
        self._add_pr(merged=True, state="closed")
        git_mock = MagicMock(return_value=_git_result(stdout=THREE_BEHIND_STDOUT))
        merge = MagicMock()
        push = MagicMock()
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            rebase=merge,
            push=push,
            git=git_mock,
        ):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)
        # No rebase, no push, no relabel.
        merge.assert_not_called()
        push.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        # Anchor cleared.
        state = self.gh.pinned_data(ISSUE)
        self.assertIsNone(state.get(KEY_PENDING_PUSH_SHA))

    def test_backlog_label_skips_pr_refresh_detour(self) -> None:
        # `backlog` is a hard skip: the refresh path must not relabel the
        # issue to `resolving_conflict` or post a PR notice while the
        # operator has the issue postponed.
        self._seed_pr_issue(extra_labels=[BACKLOG_LABEL])
        self._add_pr()
        merge = MagicMock()
        git_mock = MagicMock(return_value=_git_result(stdout=THREE_BEHIND_STDOUT))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            rebase=merge,
            git=git_mock,
        ):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_backlog_label_skips_pre_pr_base_rebase(self) -> None:
        issue = make_issue(ISSUE, label=LABEL_IMPLEMENTING)
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        self.gh.add_issue(issue)
        merge = MagicMock()
        git_mock = MagicMock(return_value=_git_result(stdout=THREE_BEHIND_STDOUT))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            rebase=merge,
            git=git_mock,
        ):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])

    def test_paused_label_skips_pr_refresh_detour(self) -> None:
        # `paused` is the same hard skip as `backlog`: the refresh path must
        # not relabel the issue to `resolving_conflict` or post a PR notice
        # while the operator has the in-flight issue frozen.
        self._seed_pr_issue(extra_labels=[PAUSED_LABEL])
        self._add_pr()
        merge = MagicMock()
        git_mock = MagicMock(return_value=_git_result(stdout=THREE_BEHIND_STDOUT))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            rebase=merge,
            git=git_mock,
        ):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_paused_label_skips_pre_pr_base_rebase(self) -> None:
        issue = make_issue(ISSUE, label=LABEL_IMPLEMENTING)
        issue.labels.append(FakeLabel(PAUSED_LABEL))
        self.gh.add_issue(issue)
        merge = MagicMock()
        git_mock = MagicMock(return_value=_git_result(stdout=THREE_BEHIND_STDOUT))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            rebase=merge,
            git=git_mock,
        ):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])

    def test_conflict_label_skips_reroute(self) -> None:
        # The handler runs this tick anyway and will do the rebase -- a
        # second label flip is pointless and would re-post the PR notice.
        self._seed_pr_issue(label=LABEL_RESOLVING_CONFLICT)
        self._add_pr()
        git_mock = MagicMock(return_value=_git_result(stdout=THREE_BEHIND_STDOUT))
        with _patch_base_sync(
            dirty=MagicMock(return_value=[]),
            git=git_mock,
        ):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, ISSUE)
        # No new label flip (the issue was already labeled
        # `resolving_conflict` at fixture time, not by us).
        self.assertEqual(self.gh.label_history, [])
        # No duplicate PR notice.
        self.assertEqual(self.gh.posted_pr_comments, [])
