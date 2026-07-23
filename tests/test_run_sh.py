# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the production restart wrapper."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.run_sh_test_support import (
    _SIGINT_EXIT_CODE,
    _TEXT_ENCODING,
    _WrapperScenario,
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
        ("skills-update", "0", False, "self-update skipped"),
        ("main", "9", True, "self-update failed"),
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
    completed = scenario.run(
        git_branch=git_branch,
        git_pull_rc=git_pull_rc,
    )

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
    assert scenario.git_calls.read_text(
        encoding=_TEXT_ENCODING,
    ).splitlines() == [
        "branch --show-current",
        "pull --ff-only origin main",
        "branch --show-current",
        "pull --ff-only origin main",
    ]
    assert scenario.sleep_calls.read_text(encoding=_TEXT_ENCODING) == "1\n"
    assert (
        "orchestrator exited with code 0; restarting in 1s"
        in completed.stdout
    )
    assert "WARNING" not in completed.stderr
