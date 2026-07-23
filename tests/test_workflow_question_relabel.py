# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator import config, workflow

from tests.fakes import FakeGitHubClient, make_issue
from tests.question_relabel_test_support import (
    RelabelCase,
    _seed_relabel,
)
from tests.workflow_helpers import (
    KEY_AWAITING_HUMAN,
    KEY_PARK_REASON,
    LABEL_IMPLEMENTING,
)
from tests.workflow_helpers import (
    _TEST_SPEC,
    _agent,
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


class QuestionRelabelToImplementingTest(
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

    def test_relabel_clears_park_and_starts_fresh(
        self,
    ) -> None:
        gh = FakeGitHubClient()
        # Issue is now labeled `implementing` (the operator relabeled)
        # but the pinned state still carries the question stage's
        # awaiting_human / park_reason from the prior tick.
        issue = make_issue(FRESH_RELABEL_ISSUE_NUMBER, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            park_reason=PARK_QUESTION_ANSWER,
            question_agent=config.DECOMPOSE_AGENT_SPEC,
            question_session_id=QUESTION_SESSION,
            last_action_comment_id=FRESH_RELABEL_COMMENT_ID,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess-1",
                last_message="implemented",
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # The dev agent ran fresh with the implement prompt (not the
        # question-stage followup), opened a PR, and flipped to
        # validating -- the relabel was honored as an unblock signal.
        mocks[RUN_AGENT].assert_called_once()
        spawn_call = mocks[RUN_AGENT].call_args
        # Fresh spawn -- no resume_session_id forwarded.
        self.assertNotIn(RESUME_SESSION_ID, spawn_call.kwargs)
        self.assertIn("You are the implementer", spawn_call.args[1])

        self.assertEqual(len(gh.opened_prs), 1)
        self.assertIn((issue.number, "validating"), gh.label_history)

        pinned_data = gh.pinned_data(issue.number)
        self.assertFalse(pinned_data.get(KEY_AWAITING_HUMAN))
        self.assertIsNone(pinned_data.get(KEY_PARK_REASON))

    def test_relabel_recovers_after_tree_reset(self) -> None:
        # After the operator resets the worktree (no commits, no dirty
        # files), the next tick goes through the safe-clear branch and
        # the dev agent runs fresh -- the unsafe-relabel park is not
        # absorbing the unblock signal.
        with tempfile.TemporaryDirectory(prefix=RELABEL_TEMP_PREFIX) as td:
            fixture = _seed_relabel(
                RelabelCase(
                    issue_number=RECOVERED_RELABEL_ISSUE_NUMBER,
                    park_reason=PARK_QUESTION_UNSAFE_RELABEL,
                    watermark=RECOVERED_RELABEL_COMMENT_ID,
                    worktree=Path(td) / f"issue-{RECOVERED_RELABEL_ISSUE_NUMBER}",
                )
            )

            mocks = self._run(
                _ImplementingStageCall(
                    fixture.github,
                    fixture.issue,
                    fixture.worktree,
                ),
                run_agent=_agent(
                    session_id="dev-sess-recovered",
                    last_message="implemented",
                ),
                # The unsafe-park branch check uses
                # `_branch_has_unpushed_commits` (default False --
                # the operator reset the local branch too) for the
                # commits half of its safety check, not the
                # worktree's `_has_new_commits`. So only two
                # `_has_new_commits` calls fire: (1) the
                # recovered-worktree check in the fresh-spawn
                # branch sees clean -> agent spawns; (2) the
                # post-agent commit check -> push path.
                has_new_commits=[False, True],
                push_branch=True,
            )

        mocks[RUN_AGENT].assert_called_once()
        self.assertNotIn(
            RESUME_SESSION_ID,
            mocks[RUN_AGENT].call_args.kwargs,
        )
        # The relabel exercises the implementing fresh-spawn path,
        # which now hands off straight to `validating` (no pre-review
        # docs hop).
        self.assertIn(
            (fixture.issue.number, "validating"),
            fixture.github.label_history,
        )
        pinned_data = fixture.github.pinned_data(fixture.issue.number)
        self.assertFalse(pinned_data.get(KEY_AWAITING_HUMAN))
        self.assertIsNone(pinned_data.get(KEY_PARK_REASON))

    def test_no_comment_relabel_runs_again(self) -> None:
        # Regression for the leak: prior to the fix, this scenario
        # would hit implementing's awaiting_human branch,
        # `_resume_developer_on_human_reply` would see no new comments
        # past the question-answer watermark, and the handler would
        # return without spawning anything. The fix clears the stale
        # question-stage park, lets the fresh-spawn branch fire, and
        # the implementation actually starts.
        gh = FakeGitHubClient()
        issue = make_issue(NO_COMMENT_RELABEL_ISSUE_NUMBER, label=LABEL_IMPLEMENTING)
        # No new human comment after the question agent's answer --
        # the operator's only signal was the relabel itself.
        gh.add_issue(issue)
        gh.seed_state(
            issue.number,
            awaiting_human=True,
            park_reason=PARK_QUESTION_SILENT,
            last_action_comment_id=NO_COMMENT_RELABEL_WATERMARK,
        )
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="needs clarification"),
            has_new_commits=False,
        )
        # Dev agent ran (the relabel was honored).
        mocks[RUN_AGENT].assert_called_once()
