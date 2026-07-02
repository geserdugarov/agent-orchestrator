# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the production restart wrapper."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
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
        """
        #!/usr/bin/env bash
        echo "$*" >> "$GIT_CALLS"
        if [ "$1" = "branch" ]; then
            printf '%s\\n' "${GIT_BRANCH:-main}"
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
    codes = " ".join(str(code) for code in (exit_codes or (130,)))
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
def test_self_update_launches_instead_of_crash_looping(
    tmp_path: Path,
    git_branch: str,
    git_pull_rc: str,
    expect_pull: bool,
    warn_substr: str | None,
) -> None:
    root = _wrapper_copy(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git_calls = tmp_path / "git-calls"
    python_calls = tmp_path / "python-calls"
    python_count = tmp_path / "python-count"

    _write_fake_git(fake_bin)
    _write_fake_python(root, 130)  # signal exit terminates the loop after one launch

    result = subprocess.run(
        ["bash", "run.sh"],
        cwd=root,
        env=_env(
            fake_bin,
            GIT_CALLS=str(git_calls),
            GIT_BRANCH=git_branch,
            GIT_PULL_RC=git_pull_rc,
            PYTHON_CALLS=str(python_calls),
            PYTHON_COUNT=str(python_count),
        ),
        capture_output=True,
        text=True,
        timeout=5,
    )

    # The orchestrator is always launched -- a self-update problem must not stop
    # the wrapper before it runs.
    assert result.returncode == 130
    assert python_calls.read_text(encoding="utf-8") == "-m orchestrator.main\n"

    git_log = git_calls.read_text(encoding="utf-8")
    if expect_pull:
        assert "pull --ff-only origin main" in git_log
    else:
        assert "pull" not in git_log
        assert "branch --show-current" in git_log

    if warn_substr is not None:
        assert warn_substr in result.stderr
        assert "running existing code" in result.stderr
    else:
        assert "WARNING" not in result.stderr


def test_clean_fast_forward_updates_on_self_modifying_restart(
    tmp_path: Path,
) -> None:
    root = _wrapper_copy(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git_calls = tmp_path / "git-calls"
    python_calls = tmp_path / "python-calls"
    python_count = tmp_path / "python-count"
    sleep_calls = tmp_path / "sleep-calls"

    _write_fake_git(fake_bin)
    _write_executable(
        fake_bin / "sleep",
        """
        #!/usr/bin/env bash
        echo "$*" >> "$SLEEP_CALLS"
        exit 0
        """,
    )
    # First run exits 0 (self-modifying merge) so the wrapper restarts; the
    # second run exits via signal so the loop terminates.
    _write_fake_python(root, 0, 130)

    result = subprocess.run(
        ["bash", "run.sh"],
        cwd=root,
        env=_env(
            fake_bin,
            GIT_CALLS=str(git_calls),
            GIT_BRANCH="main",
            GIT_PULL_RC="0",
            PYTHON_CALLS=str(python_calls),
            PYTHON_COUNT=str(python_count),
            SLEEP_CALLS=str(sleep_calls),
        ),
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 130
    # A fast-forward runs before each launch: once at startup, once on restart.
    assert git_calls.read_text(encoding="utf-8").splitlines() == [
        "branch --show-current",
        "pull --ff-only origin main",
        "branch --show-current",
        "pull --ff-only origin main",
    ]
    assert python_calls.read_text(encoding="utf-8") == (
        "-m orchestrator.main\n-m orchestrator.main\n"
    )
    assert sleep_calls.read_text(encoding="utf-8") == "1\n"
    assert "orchestrator exited with code 0; restarting in 1s" in result.stdout
    assert "WARNING" not in result.stderr
