# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator import config, worktrees

from tests.question_real_git_test_support import (
    _git_env,
    _run_git,
    _seed_target_root,
    _spec_for,
)
from tests.question_test_support import _issue_branch

REAL_GIT_SLUG = "orch__realgit"


@dataclass(frozen=True)
class CleanupFixture:
    spec: config.RepoSpec
    target: Path
    branch: str
    worktree: Path


def _branch_exists(fixture: CleanupFixture) -> bool:
    branch_probe = subprocess.run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{fixture.branch}",
        ],
        cwd=str(fixture.target),
        env=_git_env(),
        capture_output=True,
        text=True,
    )
    return branch_probe.returncode == 0


def _seed_cleanup_fixture(
    temp_root: Path,
    issue_number: int,
    *,
    create_worktree: bool,
) -> CleanupFixture:
    target, base_sha = _seed_target_root(temp_root)
    branch = _issue_branch(issue_number, slug=REAL_GIT_SLUG)
    spec = _spec_for(target)
    worktree = worktrees._worktree_path(spec, issue_number)
    if create_worktree:
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            base_sha,
            cwd=target,
        )
    else:
        _run_git("branch", branch, base_sha, cwd=target)
    return CleanupFixture(
        spec=spec,
        target=target,
        branch=branch,
        worktree=worktree,
    )
