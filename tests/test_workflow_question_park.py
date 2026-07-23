# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator import config

from tests.workflow_helpers import (
    KEY_AWAITING_HUMAN,
    KEY_PARK_REASON,
)
from tests.workflow_helpers import (
    _agent,
)

from tests.question_test_support import (
    _assert_no_pr_no_push_no_relabel,
    _dirty_files,
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
LAST_VISIBLE_FILE_INDEX = DIRTY_DISPLAY_LIMIT - 1
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


class HandleQuestionParkPathsTest(unittest.TestCase, _QuestionWorkflowMixin):
    """The handler distinguishes four park reasons -- timeout, silent
    crash, dirty worktree, and commit -- so an operator can tell why
    the conversation stalled. All four leave `awaiting_human=True`
    and no PR / no push / no relabel.
    """

    def test_timeout_parks_with_question_timeout(self) -> None:
        gh, issue = _seed_question(2)
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(timed_out=True, last_message=""),
        )
        _assert_no_pr_no_push_no_relabel(self, gh, mocks)
        pinned_data = gh.pinned_data(issue.number)
        self.assertTrue(pinned_data[KEY_AWAITING_HUMAN])
        self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_TIMEOUT)
        self.assertIn(config.HITL_MENTIONS, gh.posted_comments[-1][1])
        self.assertIn("timed out", gh.posted_comments[-1][1])

    def test_silent_run_parks_with_question_silent(self) -> None:
        # No commit AND no final message -- distinct from a real
        # clarifying question; see the implementer's `_on_question`
        # silent branch for the parallel.
        gh, issue = _seed_question(2)
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(
                last_message="",
                exit_code=1,
                stderr="something broke",
            ),
        )
        _assert_no_pr_no_push_no_relabel(self, gh, mocks)
        pinned_data = gh.pinned_data(issue.number)
        self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_SILENT)
        # Silent-path park surfaces stderr diagnostics for the operator.
        self.assertIn("something broke", gh.posted_comments[-1][1])

    def test_commit_output_parks_without_pushing(self) -> None:
        # The question stage is read-only. A commit is misbehavior --
        # park with question_commits, keep the issue on label `question`,
        # and refuse to push.
        gh, issue = _seed_question(2)
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message="here is a code change"),
            has_new_commits=True,
        )
        _assert_no_pr_no_push_no_relabel(self, gh, mocks)
        pinned_data = gh.pinned_data(issue.number)
        self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_COMMITS)
        self.assertIn("read-only", gh.posted_comments[-1][1])

    def test_dirty_worktree_parks_without_pushing(self) -> None:
        gh, issue = _seed_question(2)
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message="changes left in tree"),
            has_new_commits=False,
            dirty_files=_dirty_files(),
        )
        _assert_no_pr_no_push_no_relabel(self, gh, mocks)
        self.assertEqual(
            gh.pinned_data(issue.number)[KEY_PARK_REASON],
            PARK_QUESTION_DIRTY,
        )
        comment = gh.posted_comments[-1][1]
        self.assertIn("file_0.py", comment)
        self.assertIn(f"file_{LAST_VISIBLE_FILE_INDEX}.py", comment)
        self.assertNotIn(f"file_{DIRTY_DISPLAY_LIMIT}.py", comment)
        self.assertIn(f"({DIRTY_OVERFLOW_COUNT} more)", comment)
