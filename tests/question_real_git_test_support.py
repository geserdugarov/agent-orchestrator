# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator import config


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


@dataclass(frozen=True)
class GitBranchFixture:
    target: Path
    base_sha: str
    issue_number: int
    branch: str

    @property
    def spec(self) -> config.RepoSpec:
        return _spec_for(self.target)

    def create(self) -> None:
        _run_git(
            GIT_BRANCH,
            self.branch,
            self.base_sha,
            cwd=self.target,
        )

    def commit(self, message: str) -> None:
        self.create()
        tree = _run_git(
            GIT_REV_PARSE,
            "HEAD^{tree}",
            cwd=self.target,
        ).stdout.strip()
        new_commit = _run_git(
            "commit-tree",
            tree,
            "-p",
            self.base_sha,
            GIT_COMMIT_MESSAGE_FLAG,
            message,
            cwd=self.target,
        ).stdout.strip()
        _run_git(
            GIT_UPDATE_REF,
            f"refs/heads/{self.branch}",
            new_commit,
            cwd=self.target,
        )


def _git_env() -> dict:
    """Hermetic git env: detached from the operator's global / system
    config and with a deterministic author/committer so the test does
    not depend on the host's `~/.gitconfig`."""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "orchestrator-test",
        "GIT_AUTHOR_EMAIL": "orchestrator-test@example.invalid",
        "GIT_COMMITTER_NAME": "orchestrator-test",
        "GIT_COMMITTER_EMAIL": "orchestrator-test@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [GIT_COMMAND, *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def _seed_target_root(td: Path) -> tuple[Path, str]:
    """Initialize a temp git repo to serve as `spec.target_root`.

    Creates an initial empty commit on `main` and an `origin/main`
    remote-tracking ref pointing at it, mirroring the shape of a
    freshly-cloned repo just after `_authed_target_fetch`. Returns
    `(target_root, base_sha)` so tests can branch from it.
    """
    target = td / "target"
    target.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=target)
    _run_git(
        "commit",
        "--allow-empty",
        "-q",
        GIT_COMMIT_MESSAGE_FLAG,
        "init",
        cwd=target,
    )
    base_sha = _run_git(
        GIT_REV_PARSE,
        "HEAD",
        cwd=target,
    ).stdout.strip()
    _run_git(
        GIT_UPDATE_REF,
        "refs/remotes/origin/main",
        base_sha,
        cwd=target,
    )
    return target, base_sha


def _spec_for(target_root: Path) -> config.RepoSpec:
    return config.RepoSpec(
        slug="orch/realgit",
        target_root=target_root,
        base_branch="main",
        remote_name="origin",
    )


def _seed_branch_fixture(
    temp_root: Path,
    issue_number: int,
    branch: str,
) -> GitBranchFixture:
    target, base_sha = _seed_target_root(temp_root)
    return GitBranchFixture(
        target=target,
        base_sha=base_sha,
        issue_number=issue_number,
        branch=branch,
    )
