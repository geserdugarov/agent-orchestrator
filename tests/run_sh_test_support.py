# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Filesystem and process fixtures for the production restart wrapper."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_TEXT_ENCODING = "utf-8"
_SIGINT_EXIT_CODE = 130


def _write_executable(path: Path, text: str) -> None:
    path.write_text(
        textwrap.dedent(text).lstrip(),
        encoding=_TEXT_ENCODING,
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _wrapper_copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy2(ROOT / "run.sh", root / "run.sh")
    (root / ".venv" / "bin").mkdir(parents=True)
    return root


def _env(fake_bin: Path, **extra: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(extra)
    environment["PATH"] = (
        f"{fake_bin}{os.pathsep}{environment['PATH']}"
    )
    return environment


def _write_fake_git(fake_bin: Path) -> None:
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
    codes = " ".join(
        str(code)
        for code in (exit_codes or (_SIGINT_EXIT_CODE,))
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
