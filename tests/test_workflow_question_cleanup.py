# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path


from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    BACKEND_CLAUDE,
    KEY_PARK_REASON,
)
from tests.workflow_helpers import (
    LABEL_QUESTION,
    _TEST_SPEC,
    _agent,
)

from tests.question_test_support import (
    _issue_branch,
    _seed_question,
)
from tests.question_conversation_test_support import (
    _QuestionWorkflowMixin,
)

KEY_QUESTION_SESSION_ID = "question_session_id"

PARK_QUESTION_ANSWER = "question_answer"
PARK_QUESTION_COMMITS = "question_commits"
PARK_QUESTION_DIRTY = "question_dirty"
PARK_QUESTION_SILENT = "question_silent"
PARK_QUESTION_TIMEOUT = "question_timeout"
PARK_QUESTION_UNSAFE_RELABEL = "question_unsafe_relabel"

BRANCH_HAS_UNPUSHED_COMMITS = "_branch_has_unpushed_commits"
CLEANUP_QUESTION_WORKTREE = "_cleanup_question_worktree"
PUSH_BRANCH = "_push_branch"
RUN_AGENT = "run_agent"
WORKTREE_PATH = "_worktree_path"
RESUME_SESSION_ID = "resume_session_id"

UNEXPECTED_AGENT_MESSAGE = "should not run"
QUESTION_TEXT = "Where does X live?"
FOLLOW_UP_GUIDANCE = "please also handle empty input"
ROUND_ONE_ANSWER = "round-1 answer"
ROUND_TWO_ANSWER = "round-2 answer"

ROLLING_SESSION = "q-sess-rolling"
REAL_GIT_SLUG = "orch__realgit"
TRUSTED_AUTHOR = "geserdugarov"
OUTSIDER_AUTHOR = "mallory"
LABEL_DONE = "done"
RELABEL_TEMP_PREFIX = "q-relabel-"
GIT_COMMAND = "git"
GIT_COMMIT_MESSAGE_FLAG = "-m"
GIT_REV_PARSE = "rev-parse"
GIT_UPDATE_REF = "update-ref"
GIT_BRANCH = "branch"
MALICIOUS_URL = "https://example.invalid/malicious-patch.zip"

QUESTION_SESSION = "q-sess-prior"
QUESTION_REPLY_ID = 12000
QUESTION_REPLY_WATERMARK = 11000
DIRTY_FILE_COUNT = 15
DIRTY_DISPLAY_LIMIT = 10
DIRTY_OVERFLOW_COUNT = DIRTY_FILE_COUNT - DIRTY_DISPLAY_LIMIT
TRUSTED_REPLY_ID = QUESTION_REPLY_ID
OUTSIDER_REPLY_ID = TRUSTED_REPLY_ID + 1
MULTI_ROUND_REPLY_ID_STEP = 100
UNSAFE_PARK_WATERMARK = 88_888
MISSING_QUESTION_WORKTREE = Path("/tmp/orchestrator-test-missing-issue-86")
NO_NEW_COMMENTS_WATERMARK = 9999
CLOSED_ISSUE_NUMBER = 50
CLOSED_QUESTION_WATERMARK = 70000
CLOSED_UNSAFE_ISSUE_NUMBER = 51
CLOSED_UNSAFE_WATERMARK = 71000
CLOSED_EMPTY_ISSUE_NUMBER = 52
CLOSED_USAGE_ISSUE_NUMBER = 53
CLOSED_USAGE_TOKENS = 8800
CLOSED_USAGE_COST_USD = 0.19
ANSWER_CLEANUP_ISSUE_NUMBER = 100
SILENT_CLEANUP_ISSUE_NUMBER = 101
STALE_RESUME_ISSUE_NUMBER = 102
STALE_RESUME_WATERMARK = 99999
TIMEOUT_PARK_ISSUE_NUMBER = 103
COMMIT_PARK_ISSUE_NUMBER = 104
DIRTY_PARK_ISSUE_NUMBER = 105
UNSAFE_COMMIT_ISSUE_NUMBER = 300
UNSAFE_DIRTY_ISSUE_NUMBER = 301
UNSAFE_TIMEOUT_ISSUE_NUMBER = 302
SAFE_PARK_ISSUE_NUMBER = 303
CLEAN_REPLY_ISSUE_NUMBER = 304
CLEAN_REPLY_COMMENT_ID = 99000
REPARK_ISSUE_NUMBER = 305
REPARK_COMMENT_ID = 99500
BASE_REFRESH_ISSUE_NUMBER = 200
FRESH_RELABEL_ISSUE_NUMBER = 80
FRESH_RELABEL_COMMENT_ID = 40000
COMMITTED_RELABEL_ISSUE_NUMBER = 82
COMMITTED_RELABEL_COMMENT_ID = 60000
MISSING_TREE_RELABEL_ISSUE_NUMBER = 86
MISSING_TREE_RELABEL_COMMENT_ID = 65000
DIRTY_RELABEL_ISSUE_NUMBER = 83
DIRTY_RELABEL_COMMENT_ID = 70000
IDEMPOTENT_RELABEL_ISSUE_NUMBER = 84
IDEMPOTENT_RELABEL_COMMENT_ID = 80000
RECOVERED_RELABEL_ISSUE_NUMBER = 85
RECOVERED_RELABEL_COMMENT_ID = 90000
NO_COMMENT_RELABEL_ISSUE_NUMBER = 81
NO_COMMENT_RELABEL_WATERMARK = 50000
NO_SESSION_RESUME_ISSUE_NUMBER = 55
NO_SESSION_REPLY_ID = 42000
NO_SESSION_WATERMARK = 41000
RECOVERED_SESSION_REPLY_ID = 32000
RECOVERED_SESSION_WATERMARK = 31000
PINNED_SESSION_REPLY_ID = 22000
PINNED_SESSION_WATERMARK = 21000
FRESH_USAGE_ISSUE_NUMBER = 610
RESUMED_USAGE_ISSUE_NUMBER = 611
NO_COMMENT_USAGE_ISSUE_NUMBER = 612
INTERRUPTED_USAGE_ISSUE_NUMBER = 613
COMMITTED_INTERRUPT_ISSUE_NUMBER = 614
MISSING_BRANCH_ISSUE_NUMBER = 700
EMPTY_CLEANUP_ISSUE_NUMBER = 801
TRUSTED_RESUME_ISSUE_NUMBER = 70
TRUSTED_WATERMARK_ISSUE_NUMBER = 72
OUTSIDER_ONLY_ISSUE_NUMBER = 71


class HandleQuestionWorktreeCleanupTest(
    unittest.TestCase,
    _QuestionWorkflowMixin,
):
    """The read-only question stage must not leave a per-issue
    worktree on disk between ticks: `_refresh_base_and_worktrees`
    would otherwise merge `origin/<base>` into the pre-PR worktree,
    accreting commits on a branch the question agent is forbidden
    from touching, and a later relabel to `implementing` would then
    either trip the `question_unsafe_relabel` guard or fall through
    to the recovered-worktree push path. Every safe-exit of
    `_handle_question` therefore tears the worktree down via
    `_cleanup_question_worktree`. The unsafe parks
    (`question_commits`, `question_dirty`, `question_timeout`) keep
    the worktree so the operator can inspect.
    """

    def test_answer_path_cleans_up_worktree(self) -> None:
        gh, issue = _seed_question(ANSWER_CLEANUP_ISSUE_NUMBER)
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message="here is the answer"),
            has_new_commits=False,
        )
        mocks[CLEANUP_QUESTION_WORKTREE].assert_called_once_with(
            _TEST_SPEC,
            issue.number,
            branch=_issue_branch(issue.number),
        )

    def test_silent_path_cleans_up_worktree(self) -> None:
        gh, issue = _seed_question(SILENT_CLEANUP_ISSUE_NUMBER)
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message="", exit_code=1),
            has_new_commits=False,
        )
        mocks[CLEANUP_QUESTION_WORKTREE].assert_called_once_with(
            _TEST_SPEC,
            issue.number,
            branch=_issue_branch(issue.number),
        )

    def test_no_comments_resume_cleans_stale_tree(
        self,
    ) -> None:
        # A no-reply tick must still tear down any worktree left by
        # a prior tick. Without this, an answered question that the
        # operator left alone for a few ticks would accumulate base
        # merges in the worktree even though `_handle_question`
        # itself did nothing.
        gh = FakeGitHubClient()
        issue = make_issue(STALE_RESUME_ISSUE_NUMBER, label=LABEL_QUESTION)
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            last_action_comment_id=STALE_RESUME_WATERMARK,
            question_agent=BACKEND_CLAUDE,
            question_session_id="q-sess-stale",
            park_reason=PARK_QUESTION_ANSWER,
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
        )
        mocks[RUN_AGENT].assert_not_called()
        mocks[CLEANUP_QUESTION_WORKTREE].assert_called_once_with(
            _TEST_SPEC,
            issue.number,
            branch=_issue_branch(issue.number),
        )

    def test_timeout_park_keeps_worktree(self) -> None:
        gh, issue = _seed_question(TIMEOUT_PARK_ISSUE_NUMBER)
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(timed_out=True, last_message=""),
        )
        mocks[CLEANUP_QUESTION_WORKTREE].assert_not_called()
        pinned_data = gh.pinned_data(issue.number)
        self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_TIMEOUT)
        self.assertIn("worktree is left intact", gh.posted_comments[-1][1])

    def test_commit_park_keeps_worktree(self) -> None:
        gh, issue = _seed_question(COMMIT_PARK_ISSUE_NUMBER)
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message="here is a code change"),
            has_new_commits=True,
        )
        mocks[CLEANUP_QUESTION_WORKTREE].assert_not_called()
        pinned_data = gh.pinned_data(issue.number)
        self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_COMMITS)

    def test_dirty_park_keeps_worktree_for_inspection(self) -> None:
        gh, issue = _seed_question(DIRTY_PARK_ISSUE_NUMBER)
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message="dropped changes"),
            has_new_commits=False,
            dirty_files=["src/x.py"],
        )
        mocks[CLEANUP_QUESTION_WORKTREE].assert_not_called()
        pinned_data = gh.pinned_data(issue.number)
        self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_DIRTY)
