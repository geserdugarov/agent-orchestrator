# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import workflow

from tests.workflow_helpers import _agent

WORKTREE_PATH = "_worktree_path"
GIT_HARDENED = "_git_hardened"
GIT_REV_LIST = "rev-list"
GIT_RESET = "reset"
GIT_HARD_RESET = "--hard"
GIT_CLEAN = "clean"
GIT_CLEAN_FLAGS = "-fd"


@dataclass(frozen=True)
class DriftRunCapture:
    mocks: dict
    git_hardened: MagicMock
    worktree: Path


def _git_hardened_mock(probe_stdout: str) -> MagicMock:
    return MagicMock(
        side_effect=[
            MagicMock(returncode=0, stdout=probe_stdout, stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ],
    )


def _run_with_git(
    case,
    github,
    issue,
    git_hardened: MagicMock,
    **run_options,
) -> DriftRunCapture:
    with tempfile.TemporaryDirectory() as worktree_dir:
        worktree = Path(worktree_dir)
        with (
            patch.object(workflow, WORKTREE_PATH, return_value=worktree),
            patch.object(workflow, GIT_HARDENED, git_hardened),
        ):
            mocks = case._run_documenting(
                github,
                issue,
                run_agent=_agent(),
                push_branch=True,
                head_shas=[],
                **run_options,
            )
    return DriftRunCapture(
        mocks=mocks,
        git_hardened=git_hardened,
        worktree=worktree,
    )


def _run_drift_reconcile(
    case,
    github,
    issue,
    *,
    probe_stdout: str,
    dirty_files=(),
) -> DriftRunCapture:
    return _run_with_git(
        case,
        github,
        issue,
        _git_hardened_mock(probe_stdout),
        dirty_files=dirty_files,
    )


def _assert_reconcile_calls(
    case,
    capture: DriftRunCapture,
    remote_branch: str,
) -> None:
    case.assertEqual(capture.git_hardened.call_count, 3)
    probe_call, reset_call, clean_call = capture.git_hardened.call_args_list
    case.assertEqual(probe_call.args[0], GIT_REV_LIST)
    case.assertIn("--count", probe_call.args)
    case.assertEqual(probe_call.kwargs.get("cwd"), capture.worktree)
    case.assertEqual(reset_call.args[:2], (GIT_RESET, GIT_HARD_RESET))
    case.assertEqual(reset_call.args[2], remote_branch)
    case.assertEqual(reset_call.kwargs.get("cwd"), capture.worktree)
    case.assertEqual(clean_call.args, (GIT_CLEAN, GIT_CLEAN_FLAGS))
    case.assertEqual(clean_call.kwargs.get("cwd"), capture.worktree)
