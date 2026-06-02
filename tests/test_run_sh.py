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


def test_startup_pull_failure_stops_before_launching_orchestrator(
    tmp_path: Path,
) -> None:
    root = _wrapper_copy(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git_calls = tmp_path / "git-calls"
    python_calls = tmp_path / "python-calls"

    _write_executable(
        fake_bin / "git",
        """
        #!/usr/bin/env bash
        echo "$*" >> "$GIT_CALLS"
        exit 7
        """,
    )
    _write_executable(
        root / ".venv" / "bin" / "python",
        """
        #!/usr/bin/env bash
        echo "$*" >> "$PYTHON_CALLS"
        exit 0
        """,
    )

    result = subprocess.run(
        ["bash", "run.sh"],
        cwd=root,
        env=_env(
            fake_bin,
            GIT_CALLS=str(git_calls),
            PYTHON_CALLS=str(python_calls),
        ),
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 7
    assert git_calls.read_text(encoding="utf-8") == "pull --ff-only origin main\n"
    assert not python_calls.exists()
    assert "self-update failed" in result.stderr
    assert "stopping wrapper" in result.stderr


def test_restart_pull_failure_exits_instead_of_relaunching_old_code(
    tmp_path: Path,
) -> None:
    root = _wrapper_copy(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git_calls = tmp_path / "git-calls"
    git_count = tmp_path / "git-count"
    python_calls = tmp_path / "python-calls"
    sleep_calls = tmp_path / "sleep-calls"

    _write_executable(
        fake_bin / "git",
        """
        #!/usr/bin/env bash
        count=0
        if [ -f "$GIT_COUNT" ]; then
            count=$(cat "$GIT_COUNT")
        fi
        count=$((count + 1))
        echo "$count" > "$GIT_COUNT"
        echo "$*" >> "$GIT_CALLS"
        if [ "$count" -eq 1 ]; then
            exit 0
        fi
        exit 9
        """,
    )
    _write_executable(
        fake_bin / "sleep",
        """
        #!/usr/bin/env bash
        echo "$*" >> "$SLEEP_CALLS"
        exit 0
        """,
    )
    _write_executable(
        root / ".venv" / "bin" / "python",
        """
        #!/usr/bin/env bash
        echo "$*" >> "$PYTHON_CALLS"
        exit 0
        """,
    )

    result = subprocess.run(
        ["bash", "run.sh"],
        cwd=root,
        env=_env(
            fake_bin,
            GIT_CALLS=str(git_calls),
            GIT_COUNT=str(git_count),
            PYTHON_CALLS=str(python_calls),
            SLEEP_CALLS=str(sleep_calls),
        ),
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 9
    assert git_calls.read_text(encoding="utf-8").splitlines() == [
        "pull --ff-only origin main",
        "pull --ff-only origin main",
    ]
    assert python_calls.read_text(encoding="utf-8") == "-m orchestrator.main\n"
    assert sleep_calls.read_text(encoding="utf-8") == "1\n"
    assert "orchestrator exited with code 0; restarting in 1s" in result.stdout
    assert "self-update failed" in result.stderr
    assert "stopping wrapper" in result.stderr
