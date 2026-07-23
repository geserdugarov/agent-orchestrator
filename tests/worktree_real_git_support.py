# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from orchestrator import base_sync, config, worktrees

from tests.worktree_serialization_support import (
    _local_fetch,
    _run_git,
    _start_and_join,
)

GIT_COMMAND = "git"
BASE_BRANCH = "main"
ORIGIN_REMOTE = "origin"
REAL_GIT_TIMEOUT_SECONDS = 30.0


class _RealGitWorktreeRepo:
    def __init__(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix="orch-ensure-real-"))
        self._remote = self._tmpdir / "remote.git"
        self._work = self._tmpdir / "work"
        self._spec = config.RepoSpec(
            slug="acme/widget",
            target_root=self._work,
            base_branch=BASE_BRANCH,
            remote_name=ORIGIN_REMOTE,
        )

    def prepare(self, test_case) -> None:
        worktrees._TARGET_ROOT_LOCKS.clear()
        test_case.addCleanup(shutil.rmtree, str(self._tmpdir), ignore_errors=True)
        self._initialize_remote()
        self._seed_initial_commit()
        self._patch_runtime(test_case)

    def _initialize_remote(self) -> None:
        subprocess.run(
            [GIT_COMMAND, "init", "--bare", "-b", BASE_BRANCH, str(self._remote)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [GIT_COMMAND, "clone", str(self._remote), str(self._work)],
            check=True,
            capture_output=True,
        )

    def _seed_initial_commit(self) -> None:
        author_env = {
            "GIT_AUTHOR_NAME": "Dev",
            "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev",
            "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        (self._work / "README.md").write_text("hello\n")
        _run_git("add", ".", cwd=self._work)
        _run_git(
            "commit",
            "-m",
            "initial",
            cwd=self._work,
            env_extra=author_env,
        )
        _run_git("push", ORIGIN_REMOTE, BASE_BRANCH, cwd=self._work)

    def _patch_runtime(self, test_case) -> None:
        worktrees_patch = patch.object(
            config,
            "WORKTREES_DIR",
            self._tmpdir / "worktrees",
        )
        fetch_patch = patch.object(
            base_sync,
            "_authed_target_fetch",
            side_effect=_local_fetch,
        )
        worktrees_patch.start()
        fetch_patch.start()
        test_case.addCleanup(worktrees_patch.stop)
        test_case.addCleanup(fetch_patch.stop)


def _run_ensure_workers(test_case, recorder, issue_numbers) -> None:
    threads = [threading.Thread(target=recorder, args=(issue_number,)) for issue_number in issue_numbers]
    _start_and_join(threads, timeout=REAL_GIT_TIMEOUT_SECONDS)
    for thread in threads:
        test_case.assertFalse(
            thread.is_alive(),
            "worker timed out (possible lock contention)",
        )


def _assert_ensure_outcomes(test_case, recorder, issue_numbers) -> None:
    errors = []
    for outcome in recorder.outcomes:
        if outcome[2] is not None:
            errors.append((outcome[0], outcome[2]))
    test_case.assertEqual(errors, [])
    test_case.assertEqual(
        tuple(sorted(number for number, _, _ in recorder.outcomes)),
        issue_numbers,
    )


def _assert_worktrees_exist(test_case, recorder) -> None:
    for issue_number, worktree, _error in recorder.outcomes:
        test_case.assertIsNotNone(worktree)
        test_case.assertTrue(
            worktree.exists(),
            f"worktree {worktree} missing for issue #{issue_number}",
        )
