# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator import worktrees


from tests.question_test_support import (
    _issue_branch,
    _legacy_branch,
)
from tests.question_real_git_test_support import (
    GitBranchFixture,
    _run_git,
    _seed_branch_fixture,
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
AHEAD_BRANCH_ISSUE_NUMBER = 702
LEGACY_BRANCH_ISSUE_NUMBER = 704
DUAL_BRANCH_ISSUE_NUMBER = 705


class BranchHasUnpushedCommitsRealGitTest(unittest.TestCase):
    """Direct coverage for `_branch_has_unpushed_commits`. The stage-
    handler tests mock this helper at the `workflow` facade so they
    do not exercise the real `git rev-list` plumbing; this class
    drives the helper against a real temp-backed clone so a
    regression in the rev-list args, the lock acquisition, or the
    branch-existence pre-check surfaces here.
    """

    def test_returns_false_when_branch_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bhpc-noBranch-") as td:
            target, _ = _seed_target_root(Path(td))
            spec = _spec_for(target)
            self.assertFalse(
                worktrees._branch_has_unpushed_commits(
                    spec,
                    MISSING_BRANCH_ISSUE_NUMBER,
                ),
            )

    def test_returns_false_when_branch_at_base(self) -> None:
        # `orchestrator/orch__realgit/issue-N` exists at exactly origin/main: a
        # fresh-from-base branch has no commits to inspect.
        with tempfile.TemporaryDirectory(prefix="bhpc-atBase-") as td:
            issue_number = 701
            target, base_sha = _seed_target_root(Path(td))
            _run_git(
                GIT_BRANCH,
                _issue_branch(issue_number, slug=REAL_GIT_SLUG),
                base_sha,
                cwd=target,
            )
            spec = _spec_for(target)
            self.assertFalse(
                worktrees._branch_has_unpushed_commits(spec, issue_number),
            )

    def test_true_when_branch_ahead_of_base(
        self,
    ) -> None:
        # `orchestrator/orch__realgit/issue-N` has at least one commit beyond
        # origin/main. This is the read-only-violation we are
        # trying to detect.
        with tempfile.TemporaryDirectory(prefix="bhpc-ahead-") as td:
            fixture = _seed_branch_fixture(
                Path(td),
                AHEAD_BRANCH_ISSUE_NUMBER,
                _issue_branch(AHEAD_BRANCH_ISSUE_NUMBER, slug=REAL_GIT_SLUG),
            )
            # Add a commit on the issue branch. Update the ref
            # directly via `commit-tree` so we don't touch the
            # parent clone's checkout state.
            fixture.commit("agent commit")
            self.assertTrue(
                worktrees._branch_has_unpushed_commits(
                    fixture.spec,
                    fixture.issue_number,
                ),
            )

    def test_false_when_remote_base_missing(self) -> None:
        # If `refs/remotes/origin/main` has been pruned (a
        # mis-configured local clone, a fetch failure earlier in
        # the tick), `git rev-list` exits non-zero. The helper
        # conservatively returns None -- the caller's later steps
        # surface any persistent problem.
        with tempfile.TemporaryDirectory(prefix="bhpc-noBase-") as td:
            issue_number = 703
            target, base_sha = _seed_target_root(Path(td))
            _run_git(
                GIT_BRANCH,
                _issue_branch(issue_number, slug=REAL_GIT_SLUG),
                base_sha,
                cwd=target,
            )
            _run_git(
                GIT_UPDATE_REF,
                "-d",
                "refs/remotes/origin/main",
                cwd=target,
            )
            spec = _spec_for(target)
            self.assertIsNone(
                worktrees._branch_has_unpushed_commits(spec, issue_number),
            )

    def test_detects_legacy_issue_branch_commits(
        self,
    ) -> None:
        # Regression: a pre-slug-namespacing `question_commits` park
        # holds the question agent's commits on the legacy
        # `orchestrator/issue-N` ref. The pinned state never recorded
        # `branch` (question stage is read-only and never pushed), so
        # the resolver falls back to the slug-namespaced form -- but
        # that branch does not exist locally. Probing ONLY the
        # namespaced form would return None, the `_handle_implementing`
        # relabel guard would clear the park, `_ensure_worktree` would
        # reuse the on-disk worktree (still checked out on the legacy
        # branch), and the recovered-worktree shortcut would push the
        # question-agent commits as a fresh dev PR. The helper must
        # also probe the legacy ref and name it in the return value
        # so the operator hint targets the right branch.
        with tempfile.TemporaryDirectory(prefix="bhpc-legacy-") as td:
            fixture = _seed_branch_fixture(
                Path(td),
                LEGACY_BRANCH_ISSUE_NUMBER,
                _legacy_branch(LEGACY_BRANCH_ISSUE_NUMBER),
            )
            fixture.commit("stale question commit")
            # Slug-namespaced form does NOT exist; only the legacy
            # form does. Helper must still return the offending
            # branch name (the legacy ref) so the relabel guard fires.
            self.assertEqual(
                worktrees._branch_has_unpushed_commits(
                    fixture.spec,
                    fixture.issue_number,
                ),
                fixture.branch,
            )

    def test_namespaced_branch_wins(self) -> None:
        # Both refs carry commits (a host-restart edge case where the
        # operator force-recreated the namespaced branch without
        # reaping the legacy one). The helper must report the
        # namespaced form first -- that is the branch the rest of the
        # tick will operate on, so it is the one the operator should
        # reset.
        with tempfile.TemporaryDirectory(prefix="bhpc-both-") as td:
            namespaced = _issue_branch(
                DUAL_BRANCH_ISSUE_NUMBER,
                slug=REAL_GIT_SLUG,
            )
            primary = _seed_branch_fixture(
                Path(td),
                DUAL_BRANCH_ISSUE_NUMBER,
                namespaced,
            )
            legacy = GitBranchFixture(
                target=primary.target,
                base_sha=primary.base_sha,
                issue_number=primary.issue_number,
                branch=_legacy_branch(primary.issue_number),
            )
            primary.commit(f"c on {primary.branch}")
            legacy.commit(f"c on {legacy.branch}")
            self.assertEqual(
                worktrees._branch_has_unpushed_commits(
                    primary.spec,
                    primary.issue_number,
                ),
                namespaced,
            )
