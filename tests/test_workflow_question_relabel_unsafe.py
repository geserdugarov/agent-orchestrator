# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.question_relabel_test_support import (
    RelabelCase,
    _seed_relabel,
)
from tests.workflow_helpers import (
    KEY_AWAITING_HUMAN,
    KEY_PARK_REASON,
)
from tests.workflow_helpers import (
    _agent,
)

from tests.question_test_support import (
    _issue_branch,
)
from tests.question_conversation_test_support import (
    _ImplementingStageCall,
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


class QuestionUnsafeRelabelTest(
    unittest.TestCase,
    _QuestionWorkflowMixin,
):
    """Operator relabels a parked `question` issue to `implementing`.

    `_handle_question` parks with `awaiting_human=True` and
    `park_reason="question_*"` so its own next tick can resume the
    locked question-agent session. Those flags are opaque to
    `_handle_implementing`'s resume path; without the
    question-stage-park clear at the top of that handler, the
    awaiting_human branch either no-ops (no new comments since the
    question agent's answer) or fresh-spawns the dev with only the
    human's reply as the prompt rather than a real implement prompt.
    """

    def test_committed_state_relabel_refuses_push(
        self,
    ) -> None:
        # Regression: the operator relabels from `question` to
        # `implementing` after the question agent's prior tick parked
        # on `question_commits` with unreviewed commits in the
        # worktree. Naively clearing the question-stage park would let
        # the fresh-spawn branch's recovered-worktree shortcut push
        # those commits as if a dev session authored them, violating
        # the read-only contract. The handler must refuse and ask the
        # operator to reset the worktree first.
        with tempfile.TemporaryDirectory(prefix=RELABEL_TEMP_PREFIX) as td:
            fixture = _seed_relabel(
                RelabelCase(
                    issue_number=COMMITTED_RELABEL_ISSUE_NUMBER,
                    park_reason=PARK_QUESTION_COMMITS,
                    watermark=COMMITTED_RELABEL_COMMENT_ID,
                    worktree=Path(td) / f"issue-{COMMITTED_RELABEL_ISSUE_NUMBER}",
                )
            )

            mocks = self._run(
                _ImplementingStageCall(
                    fixture.github,
                    fixture.issue,
                    fixture.worktree,
                    unpushed_branch=_issue_branch(fixture.issue.number),
                ),
                run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
                has_new_commits=True,
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertEqual(fixture.github.opened_prs, [])
        pinned_data = fixture.github.pinned_data(fixture.issue.number)
        self.assertTrue(pinned_data[KEY_AWAITING_HUMAN])
        self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_UNSAFE_RELABEL)
        last = fixture.github.posted_comments[-1][1]
        self.assertIn(PARK_QUESTION_COMMITS, last)
        self.assertIn("reset the worktree", last.lower())

    def test_missing_tree_stale_branch_refuses_push(
        self,
    ) -> None:
        # Regression: the worktree directory is gone (a prior safe
        # park's `_cleanup_question_worktree` ran, or the operator
        # manually deleted the dir) but the local
        # `orchestrator/issue-N` branch survives with the question
        # agent's commits -- `_cleanup_question_worktree` failed
        # mid-way, or the operator removed only the dir. The
        # worktree-only check would treat the missing path as
        # "clean", let the safe-clear branch fire, and
        # `_ensure_worktree` would restore the branch in a fresh
        # worktree -- the recovered-worktree shortcut would then
        # push the question-agent commits as if a dev session
        # authored them. The branch-level check catches this.
        # Worktree path that does NOT exist on disk so wt.exists() is False.
        if MISSING_QUESTION_WORKTREE.exists():
            MISSING_QUESTION_WORKTREE.rmdir()
        fixture = _seed_relabel(
            RelabelCase(
                issue_number=MISSING_TREE_RELABEL_ISSUE_NUMBER,
                park_reason=PARK_QUESTION_COMMITS,
                watermark=MISSING_TREE_RELABEL_COMMENT_ID,
                worktree=MISSING_QUESTION_WORKTREE,
                create_worktree=False,
            )
        )

        mocks = self._run(
            _ImplementingStageCall(
                fixture.github,
                fixture.issue,
                fixture.worktree,
                unpushed_branch=_issue_branch(fixture.issue.number),
            ),
            run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
            has_new_commits=False,
            dirty_files=(),
        )

        # No dev agent ran, no push, no PR -- the branch-level
        # check refused the relabel.
        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertEqual(fixture.github.opened_prs, [])
        # State carries the unsafe-relabel park reason.
        pinned_state = fixture.github.pinned_data(fixture.issue.number)
        self.assertTrue(pinned_state[KEY_AWAITING_HUMAN])
        self.assertEqual(pinned_state[KEY_PARK_REASON], PARK_QUESTION_UNSAFE_RELABEL)
        # Message tells the operator about the branch and how to
        # reset it.
        last = fixture.github.posted_comments[-1][1]
        self.assertIn(PARK_QUESTION_COMMITS, last)
        self.assertIn(_issue_branch(fixture.issue.number), last)
        self.assertIn("git branch -D", last)

    def test_dirty_state_relabel_refuses_push(self) -> None:
        # Same as the commits case, but for `question_dirty`: the
        # question agent left uncommitted edits. Refusal must fire
        # regardless of which read-only-violation path tagged the park.
        with tempfile.TemporaryDirectory(prefix=RELABEL_TEMP_PREFIX) as td:
            fixture = _seed_relabel(
                RelabelCase(
                    issue_number=DIRTY_RELABEL_ISSUE_NUMBER,
                    park_reason=PARK_QUESTION_DIRTY,
                    watermark=DIRTY_RELABEL_COMMENT_ID,
                    worktree=Path(td) / f"issue-{DIRTY_RELABEL_ISSUE_NUMBER}",
                )
            )

            mocks = self._run(
                _ImplementingStageCall(
                    fixture.github,
                    fixture.issue,
                    fixture.worktree,
                ),
                run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
                has_new_commits=False,
                dirty_files=["src/x.py"],
            )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        pinned_data = fixture.github.pinned_data(fixture.issue.number)
        self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_UNSAFE_RELABEL)

    def test_relabel_idempotent_until_tree_reset(
        self,
    ) -> None:
        # Once the unsafe-relabel re-park has fired, subsequent ticks
        # with the same state must NOT spam a fresh park comment every
        # tick -- the operator has been informed; the only way out is
        # to reset the worktree. The clean-worktree branch fires when
        # the operator actually resets and the handler resumes the
        # normal fresh-spawn flow.
        with tempfile.TemporaryDirectory(prefix=RELABEL_TEMP_PREFIX) as td:
            fixture = _seed_relabel(
                RelabelCase(
                    issue_number=IDEMPOTENT_RELABEL_ISSUE_NUMBER,
                    park_reason=PARK_QUESTION_UNSAFE_RELABEL,
                    watermark=IDEMPOTENT_RELABEL_COMMENT_ID,
                    worktree=Path(td) / f"issue-{IDEMPOTENT_RELABEL_ISSUE_NUMBER}",
                )
            )

            mocks = self._run(
                _ImplementingStageCall(
                    fixture.github,
                    fixture.issue,
                    fixture.worktree,
                    unpushed_branch=_issue_branch(fixture.issue.number),
                ),
                run_agent=_agent(last_message=UNEXPECTED_AGENT_MESSAGE),
                has_new_commits=True,
            )

            self.assertEqual(fixture.github.posted_comments, [])
            mocks[RUN_AGENT].assert_not_called()
            pinned_data = fixture.github.pinned_data(fixture.issue.number)
            self.assertTrue(pinned_data[KEY_AWAITING_HUMAN])
            self.assertEqual(pinned_data[KEY_PARK_REASON], PARK_QUESTION_UNSAFE_RELABEL)
