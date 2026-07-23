# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from orchestrator import base_sync, config, workflow

from tests.fakes import FakeGitHubClient, FakePR, make_issue
from tests.workflow_helpers import (
    LABEL_IMPLEMENTING,
    STATE_OPEN,
)

REPO_SLUG = "acme/widget"
BASE_BRANCH = "main"
PR_BRANCH = "orchestrator/acme__widget/issue-7"
KEY_CONFLICT_ROUND = "conflict_round"
KEY_REVIEW_ROUND = "review_round"
GIT_COMMAND = "git"
ADD_COMMAND = "add"
PUSH_COMMAND = "push"
ORIGIN_REMOTE = "origin"
WORKTREES_DIR_NAME = "worktrees"
WORKTREES_DIR_ATTR = "WORKTREES_DIR"
EXTRA_FILENAME = "extra.txt"
PR_NUMBER = 42


def _branch(issue_number: int) -> str:
    return f"orchestrator/acme__widget/issue-{issue_number}"


def _local_fetch(spec, branch):
    return subprocess.run(
        [GIT_COMMAND, "fetch", "--quiet", spec.remote_name, branch],
        cwd=str(spec.target_root),
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


class _LocalBranchPusher:
    def __init__(self) -> None:
        self.branch = ""
        self.force_with_lease = ""

    def __call__(
        self,
        _spec,
        worktree,
        branch,
        *,
        force_with_lease=None,
    ) -> bool:
        self.branch = branch
        self.force_with_lease = force_with_lease or ""
        expected_lease = self.force_with_lease
        push_result = subprocess.run(
            [
                GIT_COMMAND,
                PUSH_COMMAND,
                f"--force-with-lease=refs/heads/{branch}:{expected_lease}",
                ORIGIN_REMOTE,
                f"HEAD:refs/heads/{branch}",
            ],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return push_result.returncode == 0


class _FixtureBuilder:
    def __init__(self, fixture) -> None:
        self._fixture = fixture

    def prepare(self) -> None:
        self._initialize_clone()
        self._seed_repository()
        self._initialize_worktree()
        self._configure_runtime()

    def _initialize_clone(self) -> None:
        fixture = self._fixture
        fixture._remote = fixture._tmpdir / "remote.git"
        subprocess.run(
            [
                GIT_COMMAND,
                "init",
                "--bare",
                "-b",
                BASE_BRANCH,
                str(fixture._remote),
            ],
            check=True,
            capture_output=True,
        )
        fixture._work = fixture._tmpdir / "work"
        subprocess.run(
            [GIT_COMMAND, "clone", str(fixture._remote), str(fixture._work)],
            check=True,
            capture_output=True,
        )

    def _seed_repository(self) -> None:
        fixture = self._fixture
        fixture._author_env = {
            "GIT_AUTHOR_NAME": "Dev",
            "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev",
            "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        (fixture._work / "README.md").write_text("hello\n")
        fixture._git(ADD_COMMAND, ".", cwd=fixture._work)
        fixture._git(
            "commit",
            "-m",
            "initial",
            cwd=fixture._work,
            env_extra=fixture._author_env,
        )
        fixture._git(
            PUSH_COMMAND,
            ORIGIN_REMOTE,
            BASE_BRANCH,
            cwd=fixture._work,
        )

    def _initialize_worktree(self) -> None:
        fixture = self._fixture
        fixture._wt_root = fixture._tmpdir / WORKTREES_DIR_NAME / "acme__widget"
        fixture._wt_root.mkdir(parents=True)
        fixture._wt = fixture._wt_root / "issue-7"
        fixture._git(
            "worktree",
            ADD_COMMAND,
            "-b",
            PR_BRANCH,
            str(fixture._wt),
            "origin/main",
            cwd=fixture._work,
        )
        (fixture._wt / "feature.py").write_text("feature\n")
        fixture._git(ADD_COMMAND, ".", cwd=fixture._wt)
        fixture._git(
            "commit",
            "-m",
            "feat: add feature",
            cwd=fixture._wt,
            env_extra=fixture._author_env,
        )

    def _configure_runtime(self) -> None:
        fixture = self._fixture
        fixture._spec = config.RepoSpec(
            slug=REPO_SLUG,
            target_root=fixture._work,
            base_branch=BASE_BRANCH,
        )
        fixture._gh = FakeGitHubClient()
        fixture._gh.add_issue(make_issue(7, label=LABEL_IMPLEMENTING))
        fixture._fetch_patch = patch.object(
            base_sync,
            "_authed_target_fetch",
            side_effect=_local_fetch,
        )
        fixture._fetch_patch.start()
        fixture.addCleanup(fixture._fetch_patch.stop)


class _RefreshBaseRealGitFixture:
    """Integration coverage for `_refresh_base_and_worktrees` against a real
    bare remote + per-issue worktree. Mirrors `SquashHelperRealGitTest`'s
    setup so the helper's interaction with `git fetch` / `git rebase` /
    `git rebase --abort` is exercised end-to-end.
    """

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix="orch-refresh-real-"))
        self.addCleanup(
            shutil.rmtree,
            str(self._tmpdir),
            ignore_errors=True,
        )
        _FixtureBuilder(self).prepare()

    def _git(self, *args: str, cwd: Path, env_extra: dict | None = None) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if env_extra:
            env.update(env_extra)
        git_result = subprocess.run(
            [GIT_COMMAND, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return git_result.stdout

    def _seed_pr_state(
        self,
        issue_number: int,
        pr_number: int = 999,
        *,
        merged: bool = False,
        state: str = STATE_OPEN,
    ) -> None:
        self._gh.seed_state(
            issue_number,
            pr_number=pr_number,
            branch=_branch(issue_number),
        )
        self._gh.add_pr(
            FakePR(
                number=pr_number,
                head_branch=_branch(issue_number),
                merged=merged,
                state=state,
            )
        )

    def _advance_base(self, *, conflicting: bool) -> None:
        """Push a new commit to origin/main. When `conflicting=True`, the
        commit edits `feature.py` so a base rebase of the per-issue branch
        will conflict with the local feature commit.
        """
        self._git("checkout", BASE_BRANCH, cwd=self._work)
        filename = "feature.py" if conflicting else EXTRA_FILENAME
        path = self._work / filename
        path.write_text("base side\n")
        self._git(ADD_COMMAND, ".", cwd=self._work)
        self._git(
            "commit",
            "-m",
            "base advance",
            cwd=self._work,
            env_extra=self._author_env,
        )
        self._git(PUSH_COMMAND, ORIGIN_REMOTE, BASE_BRANCH, cwd=self._work)

    def _wt_head(self) -> str:
        return self._git("rev-parse", "HEAD", cwd=self._wt).strip()

    def _is_clean(self) -> bool:
        return self._git("status", "--porcelain", cwd=self._wt).strip() == ""

    def _refresh(self) -> None:
        with patch.object(
            workflow.config,
            WORKTREES_DIR_ATTR,
            self._tmpdir / WORKTREES_DIR_NAME,
        ):
            workflow._refresh_base_and_worktrees(self._gh, self._spec)
