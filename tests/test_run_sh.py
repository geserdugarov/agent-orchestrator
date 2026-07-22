# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the production restart wrapper."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
_TEXT_ENCODING = "utf-8"
_SIGINT_EXIT_CODE = 130


def _write_executable(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip(), encoding=_TEXT_ENCODING)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _wrapper_copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy2(ROOT / "run.sh", root / "run.sh")
    (root / ".venv" / "bin").mkdir(parents=True)
    return root


def _env(fake_bin: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(extra)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    return env


def _write_fake_git(fake_bin: Path) -> None:
    # Records every invocation and answers the two subcommands self_update runs:
    # `branch --show-current` echoes $GIT_BRANCH, `pull` exits $GIT_PULL_RC.
    _write_executable(
        fake_bin / "git",
        r"""
        #!/usr/bin/env bash
        echo "$*" >> "$GIT_CALLS"
        if [ "$1" = "branch" ]; then
            printf '%s\n' "${GIT_BRANCH:-main}"
            exit 0
        fi
        if [ "$1" = "pull" ]; then
            exit "${GIT_PULL_RC:-0}"
        fi
        exit 0
        """,
    )


def _write_fake_python(root: Path, *exit_codes: int) -> None:
    # Exits with the Nth code on the Nth launch (last code repeats), so a test
    # can drive the restart loop and still terminate deterministically.
    codes = " ".join(
        str(code) for code in (exit_codes or (_SIGINT_EXIT_CODE,))
    )
    _write_executable(
        root / ".venv" / "bin" / "python",
        f"""
        #!/usr/bin/env bash
        echo "$*" >> "$PYTHON_CALLS"
        codes=({codes})
        count=0
        [ -f "$PYTHON_COUNT" ] && count=$(cat "$PYTHON_COUNT")
        idx=$count
        [ "$idx" -ge "${{#codes[@]}}" ] && idx=$((${{#codes[@]}} - 1))
        echo "$((count + 1))" > "$PYTHON_COUNT"
        exit "${{codes[$idx]}}"
        """,
    )


def _write_fake_sleep(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "sleep",
        """
        #!/usr/bin/env bash
        echo "$*" >> "$SLEEP_CALLS"
        exit 0
        """,
    )


@dataclass(frozen=True)
class _WrapperScenario:
    root: Path
    fake_bin: Path
    git_calls: Path
    python_calls: Path
    python_count: Path
    sleep_calls: Path

    @classmethod
    def create(
        cls,
        tmp_path: Path,
        *python_exit_codes: int,
        record_sleep: bool = False,
    ) -> _WrapperScenario:
        scenario = cls(
            root=_wrapper_copy(tmp_path),
            fake_bin=tmp_path / "bin",
            git_calls=tmp_path / "git-calls",
            python_calls=tmp_path / "python-calls",
            python_count=tmp_path / "python-count",
            sleep_calls=tmp_path / "sleep-calls",
        )
        scenario.fake_bin.mkdir()
        _write_fake_git(scenario.fake_bin)
        _write_fake_python(scenario.root, *python_exit_codes)
        if record_sleep:
            _write_fake_sleep(scenario.fake_bin)
        return scenario

    def run(
        self,
        *,
        git_branch: str = "main",
        git_pull_rc: str = "0",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "run.sh"],
            cwd=self.root,
            env=_env(
                self.fake_bin,
                GIT_CALLS=str(self.git_calls),
                GIT_BRANCH=git_branch,
                GIT_PULL_RC=git_pull_rc,
                PYTHON_CALLS=str(self.python_calls),
                PYTHON_COUNT=str(self.python_count),
                SLEEP_CALLS=str(self.sleep_calls),
            ),
            capture_output=True,
            text=True,
            timeout=5,
        )


def _assert_launches(
    scenario: _WrapperScenario,
    completed: subprocess.CompletedProcess[str],
    *,
    count: int,
) -> None:
    assert completed.returncode == _SIGINT_EXIT_CODE
    assert scenario.python_calls.read_text(encoding=_TEXT_ENCODING) == (
        "-m orchestrator.main\n" * count
    )


def _assert_self_update_attempt(
    scenario: _WrapperScenario,
    *,
    expect_pull: bool,
) -> None:
    git_log = scenario.git_calls.read_text(encoding=_TEXT_ENCODING)
    if expect_pull:
        assert "pull --ff-only origin main" in git_log
    else:
        assert "pull" not in git_log
        assert "branch --show-current" in git_log


def _assert_warning(
    completed: subprocess.CompletedProcess[str],
    warning: str | None,
) -> None:
    if warning is None:
        assert "WARNING" not in completed.stderr
    else:
        assert warning in completed.stderr
        assert "running existing code" in completed.stderr


@pytest.mark.parametrize(
    "git_branch, git_pull_rc, expect_pull, warn_substr",
    [
        # Non-base branch checked out: skip the pull entirely, warn, launch.
        ("skills-update", "0", False, "self-update skipped"),
        # Base branch but diverged from origin (ff pull fails): warn, launch.
        ("main", "9", True, "self-update failed"),
        # Clean fast-forward: pull runs, launch, no warning.
        ("main", "0", True, None),
    ],
)
def test_self_update_launches_without_crash_loop(
    tmp_path: Path,
    git_branch: str,
    git_pull_rc: str,
    expect_pull: bool,
    warn_substr: str | None,
) -> None:
    scenario = _WrapperScenario.create(tmp_path, _SIGINT_EXIT_CODE)
    completed = scenario.run(git_branch=git_branch, git_pull_rc=git_pull_rc)

    _assert_launches(scenario, completed, count=1)
    _assert_self_update_attempt(scenario, expect_pull=expect_pull)
    _assert_warning(completed, warn_substr)


def test_self_restart_applies_clean_fast_forward(
    tmp_path: Path,
) -> None:
    scenario = _WrapperScenario.create(
        tmp_path,
        0,
        _SIGINT_EXIT_CODE,
        record_sleep=True,
    )
    completed = scenario.run()

    _assert_launches(scenario, completed, count=2)
    # A fast-forward runs before each launch: once at startup, once on restart.
    assert scenario.git_calls.read_text(encoding=_TEXT_ENCODING).splitlines() == [
        "branch --show-current",
        "pull --ff-only origin main",
        "branch --show-current",
        "pull --ff-only origin main",
    ]
    assert scenario.sleep_calls.read_text(encoding=_TEXT_ENCODING) == "1\n"
    assert "orchestrator exited with code 0; restarting in 1s" in completed.stdout
    assert "WARNING" not in completed.stderr
