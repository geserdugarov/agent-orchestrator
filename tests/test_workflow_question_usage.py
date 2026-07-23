# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path


from tests.fakes import FakeComment, FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    BACKEND_CLAUDE,
    KEY_AWAITING_HUMAN,
    KEY_ISSUE_AGENT_RUNS,
    KEY_ISSUE_TOTAL_TOKENS,
    KEY_PARK_REASON,
)
from tests.workflow_helpers import (
    LABEL_QUESTION,
    _agent,
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


class HandleQuestionRunUsageAccumulationTest(
    unittest.TestCase,
    _QuestionWorkflowMixin,
):
    """`_handle_question` folds each real question-agent exit into the
    per-issue usage counters, at both the fresh-spawn and awaiting-human
    resume sites, and leaves them unpersisted when the run was interrupted
    (empty stdout parses to a `no-usage` metric: a counted run with zero
    tokens).
    """

    def test_fresh_run_persists_one_run(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            FRESH_USAGE_ISSUE_NUMBER,
            label=LABEL_QUESTION,
            body=QUESTION_TEXT,
        )
        gh.add_issue(issue)

        self._run_question(
            gh,
            issue,
            run_agent=_agent(session_id="q-sess", last_message="X is in x.py."),
        )

        pinned_data = gh.pinned_data(issue.number)
        self.assertEqual(pinned_data[KEY_ISSUE_AGENT_RUNS], 1)
        self.assertEqual(pinned_data[KEY_ISSUE_TOTAL_TOKENS], 0)
        self.assertEqual(pinned_data["issue_cost_sources"], ["no-usage"])

    def test_resume_counts_one_exit(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(RESUMED_USAGE_ISSUE_NUMBER, label=LABEL_QUESTION)
        issue.comments.append(
            FakeComment(id=QUESTION_REPLY_ID, body="please clarify"),
        )
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            last_action_comment_id=QUESTION_REPLY_WATERMARK,
            question_agent=BACKEND_CLAUDE,
            question_session_id=QUESTION_SESSION,
            park_reason=PARK_QUESTION_ANSWER,
        )

        self._run_question(
            gh,
            issue,
            run_agent=_agent(
                session_id=QUESTION_SESSION,
                last_message="here you go",
            ),
        )

        # Exactly one real resume exit folded.
        self.assertEqual(
            gh.pinned_data(issue.number)[KEY_ISSUE_AGENT_RUNS],
            1,
        )

    def test_no_comment_resume_keeps_counters(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(NO_COMMENT_USAGE_ISSUE_NUMBER, label=LABEL_QUESTION)
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            last_action_comment_id=NO_NEW_COMMENTS_WATERMARK,
            question_agent=BACKEND_CLAUDE,
            question_session_id=QUESTION_SESSION,
            park_reason=PARK_QUESTION_ANSWER,
        )

        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(),
        )

        # No reply -> the resume returns before spawning, so no run is
        # counted and no counter key is created.
        mocks[RUN_AGENT].assert_not_called()
        pinned_data = gh.pinned_data(issue.number)
        self.assertNotIn(KEY_ISSUE_AGENT_RUNS, pinned_data)
        self.assertNotIn(KEY_ISSUE_TOTAL_TOKENS, pinned_data)

    def test_interrupted_run_keeps_counters_clear(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(INTERRUPTED_USAGE_ISSUE_NUMBER, label=LABEL_QUESTION)
        gh.add_issue(issue)

        self._run_question(
            gh,
            issue,
            run_agent=_agent(
                session_id="",
                last_message="",
                exit_code=1,
                interrupted=True,
            ),
        )

        # A shutdown-killed question agent returns before
        # `write_pinned_state`, so neither the folded counters nor a silent
        # park reach GitHub.
        pinned_data = gh.pinned_data(issue.number)
        self.assertNotIn(KEY_ISSUE_AGENT_RUNS, pinned_data)
        self.assertNotIn(KEY_ISSUE_TOTAL_TOKENS, pinned_data)
        self.assertFalse(pinned_data.get(KEY_AWAITING_HUMAN))
        self.assertNotEqual(pinned_data.get(KEY_PARK_REASON), PARK_QUESTION_SILENT)
        self.assertEqual(gh.posted_comments, [])

    def test_committed_interrupt_parks_no_counters(self) -> None:
        # A killed question agent that ALSO left commits still hits the
        # read-only `question_commits` park (which writes pinned state and
        # keeps the worktree for inspection). Because that write path fires,
        # the usage fold must be skipped for the interrupted run or a counter
        # would persist despite the run being killed.
        gh = FakeGitHubClient()
        issue = make_issue(COMMITTED_INTERRUPT_ISSUE_NUMBER, label=LABEL_QUESTION)
        gh.add_issue(issue)

        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(
                session_id="q-sess",
                last_message="",
                interrupted=True,
            ),
            has_new_commits=True,
        )

        pinned_data = gh.pinned_data(issue.number)
        self.assertEqual(pinned_data.get(KEY_PARK_REASON), PARK_QUESTION_COMMITS)
        # Worktree kept for inspection (the commits park's contract).
        mocks[CLEANUP_QUESTION_WORKTREE].assert_not_called()
        # The park wrote pinned state, but the killed run's usage was NOT
        # folded, so no counter accrued.
        self.assertNotIn(KEY_ISSUE_AGENT_RUNS, pinned_data)
        self.assertNotIn(KEY_ISSUE_TOTAL_TOKENS, pinned_data)
