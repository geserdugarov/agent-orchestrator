# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path


from tests.fakes import FakeComment, FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    BACKEND_CLAUDE,
    KEY_AWAITING_HUMAN,
    KEY_LAST_ACTION_COMMENT_ID,
    KEY_PARK_REASON,
)
from tests.workflow_helpers import (
    LABEL_QUESTION,
    _agent,
)

from tests.question_conversation_test_support import (
    _assert_fresh_round,
    _assert_resumed_round,
    _QuestionConversation,
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


class HandleQuestionAwaitingHumanResumeTest(
    unittest.TestCase,
    _QuestionWorkflowMixin,
):
    """Once the agent has parked awaiting human, a new comment on the
    issue resumes the locked-backend session with the human's reply
    and re-posts the next answer. No reply means the handler returns
    without spawning the agent.
    """

    def test_no_new_comments_returns_without_spawning(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(3, label=LABEL_QUESTION)
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
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
        )
        mocks[RUN_AGENT].assert_not_called()
        # No fresh comment, no relabel, no PR.
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.opened_prs, [])

    def test_new_comment_resumes_and_bumps_watermark(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(4, label=LABEL_QUESTION)
        # Human reply with id strictly greater than the prior watermark.
        issue.comments.append(
            FakeComment(id=QUESTION_REPLY_ID, body="please clarify Y"),
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
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(
                session_id=QUESTION_SESSION,
                last_message="Y is defined in y.py.",
            ),
        )
        # Resume hit the locked session id of the prior tick.
        spawn_call = mocks[RUN_AGENT].call_args
        self.assertEqual(
            spawn_call.kwargs.get(RESUME_SESSION_ID),
            QUESTION_SESSION,
        )
        # The resume prompt (positional arg 1) quotes the human's reply
        # so the agent has the new context inline.
        self.assertIn("please clarify Y", spawn_call.args[1])
        # Watermark advanced past the consumed comment so the next tick
        # without a new reply is a no-op.
        pinned_data = gh.pinned_data(issue.number)
        self.assertGreaterEqual(pinned_data[KEY_LAST_ACTION_COMMENT_ID], QUESTION_REPLY_ID)
        # The follow-up answer was posted and the issue re-parks awaiting
        # human (so the human can either answer again or close / relabel).
        self.assertTrue(pinned_data[KEY_AWAITING_HUMAN])
        self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_ANSWER)
        self.assertIn("Y is defined in y.py.", gh.posted_comments[-1][1])

    def test_multi_round_qa_advances_each_tick(self) -> None:
        # Three-round conversation: fresh spawn answers Q1, human asks
        # Q2, agent answers Q2, human asks Q3, agent answers Q3.
        # Each round the watermark must advance past the orchestrator's
        # OWN answer comment so the next no-reply tick is a no-op (i.e.
        # bot comments do not feed back into the resume loop) AND past
        # the consumed human comment so the same reply is not replayed.
        conversation = _QuestionConversation()

        first_round = conversation.answer(self, ROUND_ONE_ANSWER)
        _assert_fresh_round(self, first_round)
        conversation.assert_no_reply_is_a_noop(self)

        second_round = conversation.answer(
            self,
            ROUND_TWO_ANSWER,
            human_reply="follow-up Q2",
        )
        _assert_resumed_round(
            self,
            second_round,
            previous_watermark=first_round.watermark,
            human_reply="follow-up Q2",
            excluded_answers=(ROUND_ONE_ANSWER,),
        )

        third_round = conversation.answer(
            self,
            "round-3 answer",
            human_reply="follow-up Q3",
        )
        _assert_resumed_round(
            self,
            third_round,
            previous_watermark=second_round.watermark,
            human_reply="follow-up Q3",
            excluded_answers=(ROUND_ONE_ANSWER, ROUND_TWO_ANSWER),
        )
        conversation.assert_answers_posted_once(
            self,
            (ROUND_ONE_ANSWER, ROUND_TWO_ANSWER, "round-3 answer"),
        )
