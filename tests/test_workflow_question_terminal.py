# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator import config

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    LABEL_QUESTION,
    _TEST_SPEC,
    _agent,
)

from tests.question_test_support import (
    _issue_branch,
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


def _receipt_projection(gh, issue):
    receipts = [
        body
        for issue_number, body in gh.posted_comments
        if issue_number == issue.number and body.startswith(":receipt:")
    ]
    receipt_comment = next(comment for comment in issue.comments if comment.body.startswith(":receipt:"))
    return receipts, receipt_comment


class HandleQuestionClosedIssueTerminalTest(
    unittest.TestCase,
    _QuestionWorkflowMixin,
):
    """A human closing a `question`-labeled issue is the terminal
    signal: `_handle_question` must NOT spawn the agent, must stamp
    terminal state, flip the workflow label to `done`, and clean up
    the per-issue worktree + local branch via
    `_cleanup_question_worktree`.

    The closed-issue sweep in `list_pollable_issues` is what surfaces
    the closed `question` issue here; once we flip the label to `done`
    the sweep no longer yields it and the cost stays bounded in
    steady state.
    """

    def test_closed_skips_agent_and_finishes_done(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(CLOSED_ISSUE_NUMBER, label=LABEL_QUESTION)
        issue.closed = True
        gh.add_issue(issue)
        # Mid-conversation state from a prior tick; the close is the
        # terminal signal regardless of where the conversation was.
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            last_action_comment_id=CLOSED_QUESTION_WATERMARK,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id=QUESTION_SESSION,
            park_reason=PARK_QUESTION_ANSWER,
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
        )
        mocks[RUN_AGENT].assert_not_called()
        # No new comment posted, no PR, no resume.
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.opened_prs, [])
        # Workflow label flipped to `done`.
        self.assertEqual(gh.label_history, [(issue.number, LABEL_DONE)])
        # Terminal stamp in pinned state.
        pinned_data = gh.pinned_data(issue.number)
        self.assertIn("question_closed_at", pinned_data)
        # Cleanup ran.
        mocks[CLEANUP_QUESTION_WORKTREE].assert_called_once_with(
            _TEST_SPEC,
            issue.number,
            branch=_issue_branch(issue.number),
        )

    def test_unsafe_park_closed_still_cleans(self) -> None:
        # When the operator closes an issue parked with an unsafe
        # park reason (commits / dirty / timeout left the worktree
        # intact for inspection), closing IS the operator's "I'm
        # done with this" signal -- the inspection window ends and
        # cleanup runs unconditionally.
        gh = FakeGitHubClient()
        issue = make_issue(CLOSED_UNSAFE_ISSUE_NUMBER, label=LABEL_QUESTION)
        issue.closed = True
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            park_reason=PARK_QUESTION_COMMITS,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id=QUESTION_SESSION,
            last_action_comment_id=CLOSED_UNSAFE_WATERMARK,
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
        )
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.label_history, [(issue.number, LABEL_DONE)])
        mocks[CLEANUP_QUESTION_WORKTREE].assert_called_once_with(
            _TEST_SPEC,
            issue.number,
            branch=_issue_branch(issue.number),
        )

    def test_closed_without_state_finishes_cleanly(self) -> None:
        # No pinned state at all -- e.g. the issue was labeled
        # `question` and immediately closed before the orchestrator
        # spawned anything. The terminal handler still finalizes
        # cleanly: no agent spawn, label flips to `done`, cleanup
        # runs (idempotent best-effort if nothing exists on disk).
        gh = FakeGitHubClient()
        issue = make_issue(CLOSED_EMPTY_ISSUE_NUMBER, label=LABEL_QUESTION)
        issue.closed = True
        gh.add_issue(issue)
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
        )
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.label_history, [(issue.number, LABEL_DONE)])
        pinned_data = gh.pinned_data(issue.number)
        self.assertIn("question_closed_at", pinned_data)
        mocks[CLEANUP_QUESTION_WORKTREE].assert_called_once_with(
            _TEST_SPEC,
            issue.number,
            branch=_issue_branch(issue.number),
        )

    def test_closed_with_counters_posts_usage_verdict(
        self,
    ) -> None:
        # A Q&A thread that ran the question agent accrued usage counters;
        # the terminal close surfaces the cumulative verdict as a tracked
        # comment posted before the single `write_pinned_state`.
        gh = FakeGitHubClient()
        issue = make_issue(CLOSED_USAGE_ISSUE_NUMBER, label=LABEL_QUESTION)
        issue.closed = True
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id=QUESTION_SESSION,
            issue_agent_runs=4,
            issue_total_tokens=CLOSED_USAGE_TOKENS,
            issue_total_cost_usd=CLOSED_USAGE_COST_USD,
            issue_cost_sources=["reported"],
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
        )
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.label_history, [(issue.number, LABEL_DONE)])
        receipts, receipt_comment = _receipt_projection(gh, issue)
        self.assertEqual(len(receipts), 1)
        self.assertIn(
            "this issue: 4 agent runs · 8,800 tokens · $0.19",
            receipts[0],
        )
        self.assertIn(
            receipt_comment.id,
            gh.pinned_data(issue.number).get("orchestrator_comment_ids", []),
        )
