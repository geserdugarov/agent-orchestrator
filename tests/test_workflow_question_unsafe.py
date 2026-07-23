# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator import config

from tests.fakes import FakeComment, FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    KEY_PARK_REASON,
)
from tests.workflow_helpers import (
    LABEL_QUESTION,
    _TEST_SPEC,
    _agent,
)

from tests.question_test_support import (
    _issue_branch,
    _seed_unsafe_question,
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


class HandleQuestionUnsafeParkStabilityTest(
    unittest.TestCase,
    _QuestionWorkflowMixin,
):
    """An unsafe question-stage park (`question_commits`,
    `question_dirty`, `question_timeout`) explicitly LEAVES the
    per-issue worktree on disk so the operator can inspect what the
    misbehaving agent did. A no-reply tick on that parked state
    must NOT silently tear down the inspection target: the
    awaiting-human branch returns early without producing a new
    park decision, and the `finally` block has to carry over the
    prior tick's preservation rather than reset to clean.
    """

    def test_no_reply_commit_park_keeps_tree(
        self,
    ) -> None:
        gh, issue = _seed_unsafe_question(
            UNSAFE_COMMIT_ISSUE_NUMBER,
            PARK_QUESTION_COMMITS,
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
        )
        mocks[RUN_AGENT].assert_not_called()
        mocks[CLEANUP_QUESTION_WORKTREE].assert_not_called()

    def test_no_reply_dirty_park_keeps_tree(self) -> None:
        gh, issue = _seed_unsafe_question(
            UNSAFE_DIRTY_ISSUE_NUMBER,
            PARK_QUESTION_DIRTY,
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
        )
        mocks[RUN_AGENT].assert_not_called()
        mocks[CLEANUP_QUESTION_WORKTREE].assert_not_called()

    def test_no_reply_timeout_park_keeps_tree(
        self,
    ) -> None:
        gh, issue = _seed_unsafe_question(
            UNSAFE_TIMEOUT_ISSUE_NUMBER,
            PARK_QUESTION_TIMEOUT,
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
        )
        mocks[RUN_AGENT].assert_not_called()
        mocks[CLEANUP_QUESTION_WORKTREE].assert_not_called()

    def test_no_reply_safe_park_cleans_stale_tree(
        self,
    ) -> None:
        # Counter-test: the preservation must only apply to UNSAFE
        # parks. A no-reply tick on a `question_answer` park still
        # cleans up a stale worktree from a previous tick (this is
        # what `test_resume_no_new_comments_still_cleans_stale_worktree`
        # in the cleanup-test class covers; restating it here keeps
        # the read of the stability class self-contained).
        gh, issue = _seed_unsafe_question(
            SAFE_PARK_ISSUE_NUMBER,
            PARK_QUESTION_ANSWER,
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

    def test_clean_answer_cleans_unsafe_park(
        self,
    ) -> None:
        # When the operator resets the worktree and replies, the
        # resumed agent's clean answer (no new commits / dirty)
        # ENDS the inspection window: the worktree is provably
        # safe to reap. Without the explicit `keep_worktree =
        # False` reset on the answer branch, the prior unsafe
        # park would keep preserving forever.
        gh = FakeGitHubClient()
        issue = make_issue(CLEAN_REPLY_ISSUE_NUMBER, label=LABEL_QUESTION)
        issue.comments.append(
            FakeComment(
                id=CLEAN_REPLY_COMMENT_ID,
                body="i reset the worktree, retry",
            ),
        )
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            park_reason=PARK_QUESTION_COMMITS,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id=QUESTION_SESSION,
            last_action_comment_id=UNSAFE_PARK_WATERMARK,
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(
                session_id=QUESTION_SESSION,
                last_message="ok, here is the actual answer",
            ),
            has_new_commits=False,
            dirty_files=(),
        )
        # Agent ran (human replied) and produced a clean answer.
        mocks[RUN_AGENT].assert_called_once()
        pinned_data = gh.pinned_data(issue.number)
        self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_ANSWER)
        # Worktree is now safe to reap.
        mocks[CLEANUP_QUESTION_WORKTREE].assert_called_once_with(
            _TEST_SPEC,
            issue.number,
            branch=_issue_branch(issue.number),
        )

    def test_repark_preserves_worktree(
        self,
    ) -> None:
        # When the operator replies without resetting (and the
        # leftover commits are still in the worktree), the resumed
        # agent's run lands on _has_new_commits=True and re-parks
        # as `question_commits` -- preservation continues.
        gh = FakeGitHubClient()
        issue = make_issue(REPARK_ISSUE_NUMBER, label=LABEL_QUESTION)
        issue.comments.append(
            FakeComment(id=REPARK_COMMENT_ID, body="why did you commit?"),
        )
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            park_reason=PARK_QUESTION_COMMITS,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id=QUESTION_SESSION,
            last_action_comment_id=UNSAFE_PARK_WATERMARK,
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(
                session_id=QUESTION_SESSION,
                last_message="i had to commit",
            ),
            has_new_commits=True,
        )
        mocks[RUN_AGENT].assert_called_once()
        pinned_state = gh.pinned_data(issue.number)
        self.assertEqual(pinned_state[KEY_PARK_REASON], PARK_QUESTION_COMMITS)
        mocks[CLEANUP_QUESTION_WORKTREE].assert_not_called()
