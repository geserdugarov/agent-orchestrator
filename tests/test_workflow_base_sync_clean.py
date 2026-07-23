# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from tests.base_sync_scenarios import PUSH_PATCH

import unittest
from unittest.mock import patch

from orchestrator import workflow

from tests.fakes import (
    FakePRRef,
)

from tests.base_sync_clean_assertions import (
    _assert_clean_events,
    _assert_clean_publication,
    _assert_clean_state_comments,
    _assert_conflict_publication,
    _assert_conflict_state_event,
    _assert_push_failure_git,
    _assert_push_failure_state,
)
from tests.base_sync_scenarios import (
    _clean_rebase_scenario,
    _conflict_rebase_scenario,
)

# --- Shared base-sync fixture literals -----------------------------------
# One worktree per issue drives every scenario here: issue #7 with an open
# PR #42 on the canonical head branch of the `acme/widget` target repo.
from tests.base_sync_test_support import (
    _AwaitingHumanRecorder,
    _SyncWorktreeWithBaseFixture,
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


class CleanRebaseRoutingUnitTest(_SyncWorktreeWithBaseFixture, unittest.TestCase):
    def test_in_review_rebase_routes_to_validating(self) -> None:
        self._seed_pr_issue(review_round=3)
        self._add_pr()
        scenario = _clean_rebase_scenario(THREE_BEHIND_STDOUT)

        scenario.run(self)

        _assert_clean_publication(self, self, scenario)
        _assert_clean_state_comments(self, self)
        _assert_clean_events(self, self)

    def test_conflict_rebase_routes_to_resolution(self) -> None:
        self._seed_pr_issue()
        self._add_pr(head=FakePRRef(sha=CONFLICT_PR_HEAD_SHA))
        scenario = _conflict_rebase_scenario()

        scenario.run(self)

        _assert_conflict_publication(self, self, scenario)
        _assert_conflict_state_event(self, self)

    def test_validating_rebase_stays_validating(self) -> None:
        self._seed_pr_issue(label=LABEL_VALIDATING)
        self._add_pr()
        scenario = _clean_rebase_scenario()

        scenario.run(self)

        self.assertIn((ISSUE, LABEL_VALIDATING), self.gh.label_history)
        self.assertNotIn(
            (ISSUE, LABEL_RESOLVING_CONFLICT),
            self.gh.label_history,
        )
        scenario[PUSH_PATCH].assert_called_once()

    def test_documenting_rebase_routes_to_validating(self) -> None:
        self._seed_pr_issue(label=LABEL_DOCUMENTING)
        self._add_pr()

        _clean_rebase_scenario().run(self)

        self.assertIn((ISSUE, LABEL_VALIDATING), self.gh.label_history)
        self.assertNotIn(
            (ISSUE, LABEL_RESOLVING_CONFLICT),
            self.gh.label_history,
        )

    def test_clean_push_failure_resets_and_parks(self) -> None:
        self._seed_pr_issue()
        self._add_pr()
        scenario = _clean_rebase_scenario(push_result=False)

        scenario.run(self)

        _assert_push_failure_git(self, self, scenario)
        _assert_push_failure_state(self, self)

    def test_clean_push_failure_skips_handler(self) -> None:
        self._seed_pr_issue()
        self._add_pr()
        scenario = _clean_rebase_scenario(push_result=False)
        in_review = _AwaitingHumanRecorder()

        with patch.object(
            workflow,
            "_handle_in_review",
            side_effect=in_review,
        ):
            scenario.run(self)
            workflow._process_issue(
                self.gh,
                self.spec,
                self.gh._issues[ISSUE],
            )

        self.assertEqual(in_review.observed, [True])
        self.assertEqual(self.gh.posted_pr_comments, [])
        self.assertEqual(self.gh.label_history, [])
