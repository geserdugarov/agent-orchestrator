# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import config, worktrees

from tests.question_cleanup_real_git_support import (
    _branch_exists,
    _seed_cleanup_fixture,
)
from tests.question_real_git_test_support import (
    _seed_target_root,
    _spec_for,
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
CLEANUP_WORKTREE_ISSUE_NUMBER = 800
BRANCH_ONLY_ISSUE_NUMBER = 802


class CleanupQuestionWorktreeRealGitTest(unittest.TestCase):
    """Direct coverage for `_cleanup_question_worktree` against a
    real worktree + local branch. The stage-handler tests mock this
    helper at the `workflow` facade; this class drives the real
    `git worktree remove` + `git branch -D` plumbing so a
    regression in argument order, lock acquisition, or
    error-swallowing surfaces here.
    """

    def test_removes_worktree_and_local_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cqw-both-") as td:
            # Stand up a worktree at the path `_worktree_path` will
            # compute. Patch WORKTREES_DIR so the slug-derived
            # subdirectory lives inside this temp dir.
            with patch.object(
                config,
                "WORKTREES_DIR",
                Path(td) / "wts",
            ):
                fixture = _seed_cleanup_fixture(
                    Path(td),
                    CLEANUP_WORKTREE_ISSUE_NUMBER,
                    create_worktree=True,
                )
                self.assertTrue(fixture.worktree.exists())
                # Branch should exist locally.
                self.assertTrue(_branch_exists(fixture))

                worktrees._cleanup_question_worktree(
                    fixture.spec,
                    CLEANUP_WORKTREE_ISSUE_NUMBER,
                )

                self.assertFalse(fixture.worktree.exists())
                # Local branch is gone.
                self.assertFalse(_branch_exists(fixture))

    def test_idempotent_when_nothing_exists(self) -> None:
        # No worktree on disk, no local branch -- the cleanup must
        # not raise (best-effort contract: cleanup never propagates
        # out of the handler).
        with tempfile.TemporaryDirectory(prefix="cqw-nothing-") as td:
            tdp = Path(td)
            target, _ = _seed_target_root(tdp)
            with patch.object(config, "WORKTREES_DIR", tdp / "wts"):
                spec = _spec_for(target)
                # Should not raise.
                worktrees._cleanup_question_worktree(
                    spec,
                    EMPTY_CLEANUP_ISSUE_NUMBER,
                )

    def test_missing_tree_still_deletes_branch(self) -> None:
        # The reviewer's scenario: a prior tick's worktree directory
        # was removed (manual cleanup, or partial cleanup) but the
        # local branch survived. `_cleanup_question_worktree` must
        # still tear the branch down so a later `_ensure_worktree`
        # cannot reuse it.
        with tempfile.TemporaryDirectory(prefix="cqw-branchOnly-") as td:
            with patch.object(
                config,
                "WORKTREES_DIR",
                Path(td) / "wts",
            ):
                fixture = _seed_cleanup_fixture(
                    Path(td),
                    BRANCH_ONLY_ISSUE_NUMBER,
                    create_worktree=False,
                )
                # Sanity: worktree path does not exist.
                self.assertFalse(fixture.worktree.exists())

                worktrees._cleanup_question_worktree(
                    fixture.spec,
                    BRANCH_ONLY_ISSUE_NUMBER,
                )

                self.assertFalse(_branch_exists(fixture))
