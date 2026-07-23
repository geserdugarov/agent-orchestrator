# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock


from tests.workflow_helpers import (
    _agent,
)


# --- Workflow labels this stage routes between --------------------------
from tests.documenting_test_support import (
    _FreshDocumentingFixture,
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


class HandleDocumentingFreshSafetyTest(
    unittest.TestCase,
    _FreshDocumentingFixture,
):
    def test_dirty_worktree_parks_without_push(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="docs: partial",
            ),
            push_branch=True,
            dirty_files=[README],
            head_shas=[SHA_BEFORE, SHA_AFTER],
            branch_ahead_behind=(0, 0),
        )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertNotIn((self.issue_number, VALIDATING), gh.label_history)
        state = gh.pinned_data(self.issue_number)
        self.assertTrue(state.get(AWAITING_HUMAN))
        # `_on_dirty_worktree` does NOT set a transient park_reason --
        # the worktree carries unreviewed edits and needs a human.
        last_comment = gh.posted_comments[-1][1]
        self.assertIn(UNCOMMITTED_CHANGE, last_comment)
        self.assertIn(README, last_comment)

    def test_no_change_with_dirty_files_parks(self) -> None:
        # The agent edited files but did NOT commit, then emitted
        # `DOCS: NO_CHANGE`. Accepting that would advance to validating
        # while leaving uncommitted docs edits on disk -- the reviewer
        # agent (and any later push) would silently drop them. The
        # dirty check must run BEFORE the verdict parse.
        gh, issue = self._seeded()
        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="Tweaked README in place.\nDOCS: NO_CHANGE",
            ),
            push_branch=True,
            dirty_files=[README],
            head_shas=[SHA_BEFORE, SHA_BEFORE],
            branch_ahead_behind=(0, 0),
        )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertNotIn((self.issue_number, VALIDATING), gh.label_history)
        state = gh.pinned_data(self.issue_number)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertNotIn(DOCS_VERDICT, state)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn(UNCOMMITTED_CHANGE, last_comment)
        self.assertIn(README, last_comment)

    def test_no_marker_with_dirty_files_parks(self) -> None:
        # Same shape as above but the agent ended with a question
        # instead of `DOCS: NO_CHANGE`. The dirty check must fire
        # before `_on_question`, otherwise an "agent needs your input"
        # park would silently abandon the uncommitted edits.
        gh, issue = self._seeded()
        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="What about docs/state-machine.md?",
            ),
            push_branch=True,
            dirty_files=[DOCS_ARCHITECTURE],
            head_shas=[SHA_BEFORE, SHA_BEFORE],
            branch_ahead_behind=(0, 0),
        )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertNotIn((self.issue_number, VALIDATING), gh.label_history)
        state = gh.pinned_data(self.issue_number)
        self.assertTrue(state.get(AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn(UNCOMMITTED_CHANGE, last_comment)
        self.assertIn(DOCS_ARCHITECTURE, last_comment)
        # The "agent needs your input" question park would be the
        # WRONG outcome here -- assert we did NOT take that path.
        self.assertNotIn("agent needs your input", last_comment)

    def test_silent_run_with_dirty_files_parks(self) -> None:
        # Empty final message AND dirty edits. Without the dirty
        # check, the silent-crash path (`_on_question` with
        # `agent_silent` reason) would fire and the dirty files
        # would be invisible to the operator.
        gh, issue = self._seeded()
        self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="",
                exit_code=2,
            ),
            push_branch=True,
            dirty_files=[README],
            head_shas=[SHA_BEFORE, SHA_BEFORE],
            branch_ahead_behind=(0, 0),
        )

        state = gh.pinned_data(self.issue_number)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertNotIn((self.issue_number, VALIDATING), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn(UNCOMMITTED_CHANGE, last_comment)

    def test_behind_remote_parks_before_spawn(self) -> None:
        # The local PR branch is behind `<remote>/<branch>` -- someone
        # force-pushed externally or a sibling-resolved-conflict
        # advanced the PR head. Pushing would clobber commits we
        # never saw, so refuse to spawn the agent at all.
        gh, issue = self._seeded()
        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 2),
        )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertNotIn((self.issue_number, VALIDATING), gh.label_history)
        state = gh.pinned_data(self.issue_number)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_DIVERGED)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("behind", last_comment)

    def test_fetch_failure_parks_fetch_failed(self) -> None:
        # The PR-branch fetch fails (network / auth / branch deleted).
        # Without a current `<remote>/<branch>` we cannot reason about
        # ahead/behind, and a force-push under a stale lease could
        # clobber the real remote head. Park rather than guess.
        gh, issue = self._seeded()
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
                stderr="fatal: ref not found",
            ),
        )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertNotIn((self.issue_number, VALIDATING), gh.label_history)
        state = gh.pinned_data(self.issue_number)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_FETCH_FAILED)

    def test_push_failure_parks_with_push_failed(self) -> None:
        gh, issue = self._seeded()
        self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="docs: README tweak",
            ),
            push_branch=False,
            head_shas=[SHA_BEFORE, SHA_AFTER],
            branch_ahead_behind=(0, 0),
        )

        state = gh.pinned_data(self.issue_number)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_PUSH_FAILED)
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertNotIn((self.issue_number, VALIDATING), gh.label_history)
