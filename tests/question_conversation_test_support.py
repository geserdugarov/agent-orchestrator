# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from orchestrator import workflow

from tests.fakes import FakeComment, FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    KEY_AWAITING_HUMAN,
    KEY_LAST_ACTION_COMMENT_ID,
    KEY_PARK_REASON,
)
from tests.workflow_helpers import (
    LABEL_QUESTION,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
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


def _assert_fresh_round(case, round_result: _QuestionRound) -> None:
    case.assertTrue(round_result.state[KEY_AWAITING_HUMAN])
    case.assertEqual(round_result.state[KEY_PARK_REASON], PARK_QUESTION_ANSWER)
    case.assertEqual(
        round_result.state[KEY_QUESTION_SESSION_ID],
        _QuestionConversation.session_id,
    )
    case.assertEqual(round_result.answer_comment_count, 1)
    case.assertGreaterEqual(round_result.watermark, round_result.answer_comment_id)


def _assert_resumed_round(
    case,
    round_result: _QuestionRound,
    *,
    previous_watermark: int,
    human_reply: str,
    excluded_answers: tuple[str, ...],
) -> None:
    case.assertEqual(
        round_result.resume_session_id,
        _QuestionConversation.session_id,
    )
    case.assertIn(human_reply, round_result.prompt)
    for answer in excluded_answers:
        case.assertNotIn(answer, round_result.prompt)
    case.assertTrue(round_result.state[KEY_AWAITING_HUMAN])
    case.assertEqual(round_result.state[KEY_PARK_REASON], PARK_QUESTION_ANSWER)
    case.assertGreater(round_result.watermark, previous_watermark)


@dataclass(frozen=True)
class _QuestionRound:
    state: dict
    watermark: int
    prompt: str
    resume_session_id: str | None
    answer_comment_id: int
    answer_comment_count: int


class _QuestionConversation:
    issue_number = 40
    session_id = ROLLING_SESSION

    def __init__(self) -> None:
        self.gh = FakeGitHubClient()
        self.issue = make_issue(
            self.issue_number,
            label=LABEL_QUESTION,
            body="open question?",
        )
        self.gh.add_issue(self.issue)

    def answer(
        self,
        case,
        answer: str,
        *,
        human_reply: str | None = None,
    ) -> _QuestionRound:
        if human_reply is not None:
            self.issue.comments.append(
                FakeComment(
                    id=self.watermark + MULTI_ROUND_REPLY_ID_STEP,
                    body=human_reply,
                ),
            )
        mocks = case._run_question(
            self.gh,
            self.issue,
            run_agent=_agent(
                session_id=self.session_id,
                last_message=answer,
            ),
            has_new_commits=False,
        )
        call = mocks[RUN_AGENT].call_args
        state = dict(self.gh.pinned_data(self.issue_number))
        answer_comments = self._answer_comments(answer)
        return _QuestionRound(
            state=state,
            watermark=state[KEY_LAST_ACTION_COMMENT_ID],
            prompt=call.args[1],
            resume_session_id=call.kwargs.get(RESUME_SESSION_ID),
            answer_comment_id=answer_comments[0].id,
            answer_comment_count=len(answer_comments),
        )

    @property
    def watermark(self) -> int:
        return self.gh.pinned_data(self.issue_number)[KEY_LAST_ACTION_COMMENT_ID]

    def assert_no_reply_is_a_noop(self, case) -> None:
        mocks = case._run_question(
            self.gh,
            self.issue,
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
        )
        mocks[RUN_AGENT].assert_not_called()

    def assert_answers_posted_once(
        self,
        case,
        answers: tuple[str, ...],
    ) -> None:
        bodies = [body for _, body in self.gh.posted_comments]
        counts = {answer: sum(answer in body for body in bodies) for answer in answers}
        case.assertEqual(counts, dict.fromkeys(answers, 1))

    def _answer_comments(self, answer: str) -> list[FakeComment]:
        answer_comments = []
        for comment in reversed(self.issue.comments):
            if answer in (comment.body or ""):
                answer_comments.append(comment)
        return answer_comments


class _QuestionWorkflowMixin(_PatchedWorkflowMixin):
    def _run_question(self, gh, issue, **run_options):
        return self._run(
            lambda: workflow._handle_question(gh, _TEST_SPEC, issue),
            **run_options,
        )


class _ImplementingStageCall:
    def __init__(
        self,
        gh: FakeGitHubClient,
        issue,
        worktree_path: Path,
        *,
        unpushed_branch: str | None = None,
    ) -> None:
        self._gh = gh
        self._issue = issue
        self._worktree_path = worktree_path
        self._unpushed_branch = unpushed_branch

    def __call__(self) -> None:
        worktree_patch = patch.object(
            workflow,
            WORKTREE_PATH,
            return_value=self._worktree_path,
        )
        if self._unpushed_branch is not None:
            with (
                worktree_patch,
                patch.object(
                    workflow,
                    BRANCH_HAS_UNPUSHED_COMMITS,
                    return_value=self._unpushed_branch,
                ),
            ):
                workflow._handle_implementing(self._gh, _TEST_SPEC, self._issue)
            return
        with worktree_patch:
            workflow._handle_implementing(self._gh, _TEST_SPEC, self._issue)
