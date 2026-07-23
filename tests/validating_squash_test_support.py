# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from orchestrator import branch_publication, config, workflow

from tests.fakes import make_issue
from tests.workflow_helpers import _TEST_SPEC

BASE_BRANCH_NAME = "main"
GIT_AUTHOR_NAME = "GIT_AUTHOR_NAME"
GIT_AUTHOR_EMAIL = "GIT_AUTHOR_EMAIL"
GIT_COMMITTER_NAME = "GIT_COMMITTER_NAME"
GIT_COMMITTER_EMAIL = "GIT_COMMITTER_EMAIL"
DEV_NAME = "Dev"
DEV_EMAIL = "dev@example.com"
GIT_ADD = "add"
GIT_COMMIT = "commit"
GIT_MESSAGE_FLAG = "-m"
REMOTE_NAME = "origin"
GIT_LOG = "log"
SUBJECT_FORMAT = "--pretty=%s"
BASE_BRANCH_SETTING = "BASE_BRANCH"
PUSH_BRANCH_HELPER = "_push_branch"
GIT_RESET = "reset"
HARD_RESET = "--hard"
REMOTE_BASE_REF = "origin/main"
GIT_REV_PARSE = "rev-parse"
HEAD_REF = "HEAD"


@dataclass(frozen=True)
class SquashRun:
    success: bool
    sha: str | None
    count: int
    error: str | None
    push_mock: mock.MagicMock


def run_git(*args: str, cwd: Path, env_extra: dict | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if env_extra:
        env.update(env_extra)
    completed_process = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return completed_process.stdout


def author_env() -> dict[str, str]:
    return {
        GIT_AUTHOR_NAME: DEV_NAME,
        GIT_AUTHOR_EMAIL: DEV_EMAIL,
        GIT_COMMITTER_NAME: DEV_NAME,
        GIT_COMMITTER_EMAIL: DEV_EMAIL,
    }


def commit_files(
    worktree: Path,
    messages: tuple[str, ...],
    prefix: str,
) -> None:
    for commit_index, message in enumerate(messages, start=1):
        (worktree / f"{prefix}{commit_index}.txt").write_text(
            f"{commit_index}\n",
        )
        run_git(GIT_ADD, ".", cwd=worktree)
        run_git(
            GIT_COMMIT,
            GIT_MESSAGE_FLAG,
            message,
            cwd=worktree,
            env_extra=author_env(),
        )


class _SquashRepositoryFixtureMixin:
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-squash-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)
        self._init_remote()
        self._seed_base()
        self.branch = "orchestrator/geserdugarov__agent-orchestrator/issue-9"
        self._seed_topic()

    def _init_remote(self) -> None:
        self.remote = self.tmpdir / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", BASE_BRANCH_NAME, str(self.remote)],
            check=True,
            capture_output=True,
        )
        self.work = self.tmpdir / "work"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.work)],
            check=True,
            capture_output=True,
        )

    def _seed_base(self) -> None:
        (self.work / "README.md").write_text("hello\n")
        run_git(GIT_ADD, ".", cwd=self.work)
        run_git(
            GIT_COMMIT,
            GIT_MESSAGE_FLAG,
            "initial",
            cwd=self.work,
            env_extra=author_env(),
        )
        run_git("push", REMOTE_NAME, BASE_BRANCH_NAME, cwd=self.work)

    def _seed_topic(self) -> None:
        run_git("checkout", "-b", self.branch, cwd=self.work)
        commit_files(
            self.work,
            ("fix: typo", "add foo", "add bar"),
            "f",
        )
        run_git("push", REMOTE_NAME, self.branch, cwd=self.work)
        run_git("fetch", REMOTE_NAME, cwd=self.work)


class _SquashScenarioMixin:
    def _make_issue(self, title: str = "test issue", number: int = 9):
        return make_issue(number, title=title)

    def _commits_on_branch(self) -> list[str]:
        """Subjects of all commits between origin/main and HEAD, oldest first."""
        out = run_git(
            GIT_LOG,
            "--reverse",
            SUBJECT_FORMAT,
            "origin/main..HEAD",
            cwd=self.work,
        )
        return [line for line in out.splitlines() if line.strip()]

    def _rebuild_topic(
        self,
        messages: tuple[str, ...],
        prefix: str,
    ) -> None:
        run_git(GIT_RESET, HARD_RESET, REMOTE_BASE_REF, cwd=self.work)
        commit_files(self.work, messages, prefix)

    def _rebuild_single_commit(self) -> None:
        run_git(GIT_RESET, HARD_RESET, REMOTE_BASE_REF, cwd=self.work)
        (self.work / "only.txt").write_text("only\n")
        run_git(GIT_ADD, ".", cwd=self.work)
        run_git(
            GIT_COMMIT,
            GIT_MESSAGE_FLAG,
            "feat: only one",
            cwd=self.work,
            env_extra=author_env(),
        )

    def _seed_inferred_prefix_history(self) -> None:
        run_git("checkout", BASE_BRANCH_NAME, cwd=self.work)
        commit_files(
            self.work,
            (
                "event: launch the site",
                "event: add a gala",
                "event: add a meetup",
            ),
            "e",
        )
        run_git("push", REMOTE_NAME, BASE_BRANCH_NAME, cwd=self.work)
        run_git("checkout", self.branch, cwd=self.work)
        self._rebuild_topic(
            ("tweak the layout", "polish the copy"),
            "t",
        )

    def _head_sha(self) -> str:
        return run_git(
            GIT_REV_PARSE,
            HEAD_REF,
            cwd=self.work,
        ).strip()

    def _squash(
        self,
        *,
        issue=None,
        push_result: bool = True,
        **config_overrides,
    ) -> SquashRun:
        push_mock = mock.MagicMock(return_value=push_result)
        with ExitStack() as patches:
            patches.enter_context(
                mock.patch.object(
                    config,
                    BASE_BRANCH_SETTING,
                    BASE_BRANCH_NAME,
                ),
            )
            patches.enter_context(
                mock.patch.object(
                    branch_publication,
                    PUSH_BRANCH_HELPER,
                    new=push_mock,
                ),
            )
            for setting, setting_value in config_overrides.items():
                patches.enter_context(
                    mock.patch.object(config, setting, setting_value),
                )
            raw_result = workflow._squash_and_force_push(
                _TEST_SPEC,
                self.work,
                self.branch,
                issue or self._make_issue(),
            )
        return SquashRun(*raw_result, push_mock=push_mock)


class SquashGitFixtureMixin(
    _SquashRepositoryFixtureMixin,
    _SquashScenarioMixin,
):
    """Compose repository setup with squash scenario operations."""
