# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock


from tests.workflow_helpers import (
    _agent,
)


# --- Workflow labels this stage routes between --------------------------
from tests.documenting_scenario_test_support import (
    _ParkedDocumentingFixture,
)

DOCUMENTING = "documenting"
IN_REVIEW = "in_review"
VALIDATING = "validating"

# --- Dev agent identity pinned into per-issue state ---------------------
DEV_AGENT = "codex"
DEV_SESSION = "dev-sess"

# --- Worktree HEAD SHAs threaded through the docs / recovery flows ------
SHA_BEFORE = "aaa"
SHA_AFTER = "bbb"
SHA_DOCS = "docs-sha"
SHA_RECOVERED = "recovered-sha"
SHA_PR_HEAD = "pr-head-sha"

# --- Pinned-state field keys read back from `gh.pinned_data(...)` -------
DOCS_VERDICT = "docs_verdict"
DOCS_CHECKED_SHA = "docs_checked_sha"
REVIEW_ROUND = "review_round"
PARK_REASON = "park_reason"
AWAITING_HUMAN = "awaiting_human"
LAST_ACTION_COMMENT_ID = "last_action_comment_id"

# --- Awaiting-human park reasons the docs handler writes ----------------
PARK_PUSH_FAILED = "push_failed"
PARK_AGENT_SILENT = "agent_silent"
PARK_AGENT_TIMEOUT = "agent_timeout"
PARK_DIVERGED = "diverged_branch"
PARK_FETCH_FAILED = "fetch_failed"
PARK_DIRTY = "dirty_worktree"
PARK_AGENT_QUESTION = "agent_question"
PARK_RESET_FAILED = "worktree_reset_failed"

# --- Docs verdict values persisted on a successful pass -----------------
VERDICT_UPDATED = "updated"
VERDICT_NO_CHANGE = "no_change"

# --- Repo docs paths the agent edits / the dirty guard reports ----------
README = "README.md"
DOCS_ARCHITECTURE = "docs/architecture.md"

# --- Mock keys returned by `_PatchedWorkflowMixin._run` -----------------
RUN_AGENT = "run_agent"
PUSH_BRANCH = "_push_branch"

UNCOMMITTED_CHANGE = "uncommitted change"
TRUSTED_AUTHOR = "alice"
USER_CONTENT_CHANGED = "issue body changed"
AUTHED_FETCH = "_authed_fetch"
ORIGINAL_BODY = "original body"
UPDATED_BODY_AFTER_DOCS = "updated body after prior docs commit"
WORKTREE_PATH = "_worktree_path"
GIT_HARDENED = "_git_hardened"
GIT_REV_LIST = "rev-list"
GIT_RESET = "reset"
GIT_HARD_RESET = "--hard"
GIT_CLEAN = "clean"
GIT_CLEAN_FLAGS = "-fd"
DRIFT_UNWIND_PENDING = "docs_drift_unwind_pending"

MISSING_PR_ISSUE_NUMBER = 101
PARKED_MISSING_PR_ISSUE_NUMBER = 102
COMMIT_REPLY_ISSUE_NUMBER = 401
COMMIT_REPLY_PR_NUMBER = 41
COMMIT_REPLY_COMMENT_ID = 2100
COMMIT_REPLY_WATERMARK = 2000
NO_COMMIT_REPLY_ISSUE_NUMBER = 403
NO_COMMIT_REPLY_PR_NUMBER = 43
NO_COMMIT_REPLY_COMMENT_ID = 3100
NO_COMMIT_REPLY_WATERMARK = 3000
RECOVERED_REPLY_ISSUE_NUMBER = 404
RECOVERED_REPLY_PR_NUMBER = 44
RECOVERED_REPLY_COMMENT_ID = 4100
RECOVERED_REPLY_WATERMARK = 4000
FAILED_PUSH_REPLY_ISSUE_NUMBER = 405
FAILED_PUSH_REPLY_PR_NUMBER = 45
FAILED_PUSH_REPLY_COMMENT_ID = 5100
FAILED_PUSH_REPLY_WATERMARK = 5000
NO_NEW_COMMENT_ISSUE_NUMBER = 402
NO_NEW_COMMENT_PR_NUMBER = 42
NO_NEW_COMMENT_WATERMARK = 2500
FULL_PROMPT_REPLY_ISSUE_NUMBER = 406
FULL_PROMPT_REPLY_PR_NUMBER = 46
FULL_PROMPT_REPLY_COMMENT_ID = 6100
FULL_PROMPT_REPLY_WATERMARK = 6000
NO_CHANGE_REPLY_ISSUE_NUMBER = 407
NO_CHANGE_REPLY_PR_NUMBER = 47
NO_CHANGE_REPLY_COMMENT_ID = 7100
NO_CHANGE_REPLY_WATERMARK = 7000
CONTINUE_COMMENT_ID = 9000
CONTINUE_PR_NUMBER = 47
CONTINUE_WATERMARK = 8000
CONTINUE_ISSUE_NUMBER = 730
QUESTION_CONTINUE_ISSUE_NUMBER = 731
INTERRUPTED_ISSUE_NUMBER = 202
INTERRUPTED_PR_NUMBER = 21
INTERRUPTED_RESUME_ISSUE_NUMBER = 203
INTERRUPTED_RESUME_PR_NUMBER = 23
INTERRUPTED_RESUME_COMMENT_ID = 2100
INTERRUPTED_RESUME_WATERMARK = 2000
PARKED_FIXTURE_WATERMARK = 6000
GIT_FAILURE_EXIT_CODE = 128
PENDING_UNWIND_COMMENT_ID = 999
EXTERNAL_MERGE_ISSUE_NUMBER = 180
EXTERNAL_MERGE_PR_NUMBER = 18000
CLOSED_ISSUE_NUMBER = 181
CLOSED_PR_NUMBER = 18100
FINAL_DOCS_PR_WATERMARK = 999
FINAL_DOCS_REPLY_ID = 2000
WATERMARK_ISSUE_NUMBER = 709
WATERMARK_PR_NUMBER = 73
PICKUP_COMMENT_ID = 900
PARK_COMMENT_ID = 950
HUMAN_REPLY_ID = 1100


class HandleDocumentingParkedSilenceTest(unittest.TestCase, _ParkedDocumentingFixture):
    """Already-parked issues must not re-post the park comment on
    every poll. The fetch + behind branches in particular would
    otherwise spam the issue with `fetch_failed` / `diverged_branch`
    notices each tick while the operator drafts a reply."""

    def test_no_comments_skip_fetch(
        self,
    ) -> None:
        gh, issue = self._seeded(park_reason=PARK_AGENT_QUESTION)
        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
        )

        # No fetch, no agent spawn, no posted comments. The original
        # park is preserved verbatim.
        mocks[AUTHED_FETCH].assert_not_called()
        mocks["_ensure_pr_worktree"].assert_not_called()
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])
        self.assertEqual(gh.write_state_calls, 0)

    def test_fetch_error_does_not_repark(
        self,
    ) -> None:
        # If the fetch would have failed on this tick, the parked
        # issue must still stay silent -- the fetch call must not
        # even fire.
        gh, issue = self._seeded(park_reason=PARK_AGENT_QUESTION)
        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
            authed_fetch_result=MagicMock(
                returncode=1,
                stdout="",
                stderr="would-fail",
            ),
        )

        mocks[AUTHED_FETCH].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        # The original park reason survives untouched.
        self.assertEqual(
            gh.pinned_data(self.issue_number).get(PARK_REASON),
            PARK_AGENT_QUESTION,
        )

    def test_divergence_does_not_repark(
        self,
    ) -> None:
        # Same shape for a behind-remote tick.
        gh, issue = self._seeded(park_reason=PARK_DIRTY)
        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 3),
        )

        mocks["_branch_ahead_behind"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        # Park reason is preserved -- we did NOT clobber it with
        # `diverged_branch`.
        self.assertEqual(
            gh.pinned_data(self.issue_number).get(PARK_REASON),
            PARK_DIRTY,
        )
