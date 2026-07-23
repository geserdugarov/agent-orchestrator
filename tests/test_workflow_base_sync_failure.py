# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from tests.base_sync_scenarios import PUSH_PATCH

import unittest
from unittest.mock import MagicMock


from tests.base_sync_scenarios import _scenario

# --- Shared base-sync fixture literals -----------------------------------
# One worktree per issue drives every scenario here: issue #7 with an open
# PR #42 on the canonical head branch of the `acme/widget` target repo.
from tests.base_sync_test_support import (
    _SyncWorktreeWithBaseFixture,
    _git_result,
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


class RebaseFailureRoutingUnitTest(_SyncWorktreeWithBaseFixture, unittest.TestCase):
    def test_dirty_after_rebase_resets_and_parks(self) -> None:
        self._seed_pr_issue()
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(side_effect=[[], ["scratch.py"]]),
            rebase=MagicMock(return_value=(True, [])),
            push=MagicMock(),
            head_sha=MagicMock(side_effect=[BEFORE_SHA, AFTER_SHA]),
            git=MagicMock(return_value=_git_result(stdout=TWO_BEHIND_STDOUT)),
            hardened=MagicMock(return_value=_git_result()),
        )

        scenario.run(self)

        scenario[PUSH_PATCH].assert_not_called()
        self._assert_hardened_call(
            scenario,
            (RESET_COMMAND, HARD_RESET_FLAG, BEFORE_SHA),
        )
        self._assert_hardened_call(scenario, ("clean", "-fd"))
        self._assert_park(PARK_DIRTY, "uncommitted change")

    def test_pr_rebase_failed_without_conflicts_parks(self) -> None:
        self._seed_pr_issue()
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(return_value=[]),
            rebase=MagicMock(return_value=(False, [])),
            push=MagicMock(),
            head_sha=MagicMock(return_value=BEFORE_SHA),
            git=MagicMock(return_value=_git_result(stdout=TWO_BEHIND_STDOUT)),
            hardened=MagicMock(return_value=_git_result()),
        )

        scenario.run(self)

        self._assert_hardened_call(
            scenario,
            (REBASE_COMMAND, ABORT_FLAG),
        )
        scenario[PUSH_PATCH].assert_not_called()
        self._assert_park(PARK_FAILED, "non-conflict reason")

    def _assert_hardened_call(self, scenario, prefix: tuple[str, ...]) -> None:
        matching = [
            recorded_call
            for recorded_call in scenario["hardened"].call_args_list
            if recorded_call.args[: len(prefix)] == prefix
        ]
        self.assertEqual(
            len(matching),
            1,
            scenario["hardened"].call_args_list,
        )

    def _assert_park(self, reason: str, message_fragment: str) -> None:
        self.assertEqual(self.gh.label_history, [])
        state = self.gh.pinned_data(ISSUE)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        self.assertEqual(state.get(KEY_PARK_REASON), reason)
        self.assertEqual(len(self.gh.posted_comments), 1)
        self.assertIn(message_fragment, self.gh.posted_comments[0][1])
