# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from orchestrator import agents

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import (
    LABEL_VALIDATING,
    TEST_BASE_BRANCH,
    _PatchedWorkflowMixin,
    _issue_branch,
)

ISSUE = 7
PR_NUMBER = 21
DEV_SESSION = "dev-sess"
GIT_COMMAND = "git"
QUIET_FLAG = "-q"
WORKTREE_FLAG = "-C"
GIT_CONFIG = "config"
SEED_FILE = "seed"


class RegisteredCommunicate:
    def __init__(self, process, seen):
        self.process = process
        self.seen = seen

    def __call__(self, *_args, **_kwargs):
        with agents._running_procs_lock:
            self.seen["during"] = self.process in agents._running_procs
        return "", ""


class VerifyGateFixtureMixin(_PatchedWorkflowMixin):
    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(ISSUE, label=LABEL_VALIDATING)
        gh.add_issue(issue)
        defaults = dict(
            pr_number=PR_NUMBER,
            branch=_issue_branch(ISSUE),
            codex_session_id=DEV_SESSION,
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(ISSUE, **defaults)
        return gh, issue


class VerifyCommandsFixtureMixin:
    def setUp(self) -> None:
        self.worktree = Path(tempfile.mkdtemp())
        worktree = str(self.worktree)
        # Initialize a git repo so the dirty-detection branch works.
        subprocess.run(
            [GIT_COMMAND, "init", QUIET_FLAG, "-b", TEST_BASE_BRANCH, worktree],
            check=True,
        )
        subprocess.run(
            [GIT_COMMAND, WORKTREE_FLAG, worktree, GIT_CONFIG, "user.email", "t@t"],
            check=True,
        )
        subprocess.run(
            [GIT_COMMAND, WORKTREE_FLAG, worktree, GIT_CONFIG, "user.name", "t"],
            check=True,
        )
        (self.worktree / SEED_FILE).write_text("x")
        subprocess.run(
            [GIT_COMMAND, WORKTREE_FLAG, worktree, "add", "."],
            check=True,
        )
        subprocess.run(
            [GIT_COMMAND, WORKTREE_FLAG, worktree, "commit", QUIET_FLAG, "-m", SEED_FILE],
            check=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.worktree, ignore_errors=True)
