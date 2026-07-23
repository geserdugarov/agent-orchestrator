# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator import config

from tests.fakes import FakeComment, FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    BACKEND_CODEX,
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


class HandleQuestionSessionPersistenceTest(
    unittest.TestCase,
    _QuestionWorkflowMixin,
):
    """The agent spec is persisted BEFORE the spawn so a CLI hiccup that
    surfaces no session id cannot orphan the role identity. A later
    DECOMPOSE_AGENT env flip then cannot retarget the resume at the
    wrong backend.
    """

    def test_spec_persists_without_session_id(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(5, label=LABEL_QUESTION)
        gh.add_issue(issue)
        self._run_question(
            gh,
            issue,
            run_agent=_agent(session_id="", last_message="best-effort answer"),
        )
        pinned_data = gh.pinned_data(issue.number)
        self.assertEqual(pinned_data["question_agent"], config.DECOMPOSE_AGENT_SPEC)
        # No session id was returned -- the field is absent / falsy, but
        # the role identity is still durable.
        self.assertFalse(pinned_data.get(KEY_QUESTION_SESSION_ID))

    def test_no_session_resume_uses_full_prompt(
        self,
    ) -> None:
        # Regression: when `question_session_id` is missing (a prior
        # CLI hiccup left no captured id), `_run_agent_tracked`
        # starts a FRESH agent rather than resuming an existing
        # session. The followup-only prompt assumes a live session
        # has the issue body / title / prior conversation cached;
        # passing it to a fresh agent leaves it with nothing to
        # answer against. The handler must spawn with the full
        # question prompt in this branch so the recovery run sees
        # the same context a first-tick run would.
        gh = FakeGitHubClient()
        issue = make_issue(
            NO_SESSION_RESUME_ISSUE_NUMBER,
            label=LABEL_QUESTION,
            title=QUESTION_TEXT,
            body="We need to know where X lives in the codebase.",
        )
        issue.comments.append(
            FakeComment(id=NO_SESSION_REPLY_ID, body="any progress on this?"),
        )
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            last_action_comment_id=NO_SESSION_WATERMARK,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            # No prior session id -- the prior run hiccupped.
            park_reason=PARK_QUESTION_ANSWER,
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(
                session_id="q-sess-fresh",
                last_message="X lives in src/x.py",
            ),
        )
        # The agent ran without a resume_session_id (fresh spawn).
        spawn_call = mocks[RUN_AGENT].call_args
        self.assertIsNone(spawn_call.kwargs.get(RESUME_SESSION_ID))
        # The spawn prompt is the FULL question prompt: issue body,
        # title, and conversation are all present so the fresh
        # agent has the same context a first-tick spawn would. The
        # human's new reply is included via the conversation block.
        prompt = spawn_call.args[1]
        self.assertIn(QUESTION_TEXT, prompt)
        self.assertIn(
            "We need to know where X lives in the codebase.",
            prompt,
        )
        self.assertIn("any progress on this?", prompt)
        # The fresh spawn's returned session id is captured for
        # future ticks (already covered by another test, but
        # asserting it here keeps the recovery path self-contained).
        self.assertEqual(
            gh.pinned_data(issue.number)[KEY_QUESTION_SESSION_ID],
            "q-sess-fresh",
        )

    def test_resume_persists_new_session_id(self) -> None:
        # Regression: a prior question tick that yielded no session id
        # (CLI hiccup -- empty codex `-o` file, unparseable claude line)
        # leaves `question_session_id` unset. A later resume that DOES
        # return a session id must persist it, otherwise every future
        # reply re-spawns fresh instead of continuing the locked
        # conversation.
        gh = FakeGitHubClient()
        issue = make_issue(7, label=LABEL_QUESTION)
        issue.comments.append(
            FakeComment(
                id=RECOVERED_SESSION_REPLY_ID,
                body="follow-up reply",
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            last_action_comment_id=RECOVERED_SESSION_WATERMARK,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            # No prior session id captured -- the previous run hiccupped.
            park_reason=PARK_QUESTION_ANSWER,
        )
        self._run_question(
            gh,
            issue,
            run_agent=_agent(
                session_id="q-sess-recovered",
                last_message="continued discussion",
            ),
        )
        pinned_data = gh.pinned_data(issue.number)
        self.assertEqual(pinned_data[KEY_QUESTION_SESSION_ID], "q-sess-recovered")

    def test_pinned_session_id_is_reused_on_resume(self) -> None:
        # Regression: when the issue already has a persisted spec and
        # session id, the next tick must resume that session rather
        # than spawn a fresh one against the current config.
        gh = FakeGitHubClient()
        issue = make_issue(6, label=LABEL_QUESTION)
        issue.comments.append(
            FakeComment(
                id=PINNED_SESSION_REPLY_ID,
                body="another reply",
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            last_action_comment_id=PINNED_SESSION_WATERMARK,
            question_agent=BACKEND_CODEX,
            question_session_id="codex-sess-2",
        )
        mocks = self._run_question(
            gh,
            issue,
            run_agent=_agent(
                session_id="codex-sess-2",
                last_message="continued",
            ),
        )
        self.assertEqual(
            mocks[RUN_AGENT].call_args.args[0],
            BACKEND_CODEX,
        )
        self.assertEqual(
            mocks[RUN_AGENT].call_args.kwargs.get(RESUME_SESSION_ID),
            "codex-sess-2",
        )
