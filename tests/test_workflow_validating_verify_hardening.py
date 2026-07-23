# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from orchestrator import verify

from tests.workflow_helpers import TEST_BASE_BRANCH

GIT_COMMAND = "git"
QUIET_FLAG = "-q"
GIT_CONFIG = "config"
SEED_FILE = "seed"
LEFTOVER_FILE = "leftover.txt"
EXECUTABLE_MODE = 0o755


class DrainVerifyOutputTest(unittest.TestCase):
    """`_drain_verify_output` reads a killed verify shell's buffered output.
    The first bounded drain covers the normal case; if it wedges -- a
    descendant that escaped the group is still holding the pipe fd open -- it
    escalates to `proc.kill()` and one more bounded drain, then gives up with
    empty output. Popen is faked so the wedged path is deterministic.
    """

    def test_first_drain_returns_without_extra_kill(self) -> None:
        proc = MagicMock()
        proc.communicate.return_value = ("out", "err")
        self.assertEqual(verify._drain_verify_output(proc), ("out", "err"))
        proc.kill.assert_not_called()

    def test_wedged_drain_kills_then_returns_output(self) -> None:
        proc = MagicMock()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="verify", timeout=5),
            ("late-out", "late-err"),
        ]
        self.assertEqual(
            verify._drain_verify_output(proc),
            ("late-out", "late-err"),
        )
        proc.kill.assert_called_once()

    def test_both_drains_time_out_returns_empty(self) -> None:
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd="verify",
            timeout=5,
        )
        self.assertEqual(verify._drain_verify_output(proc), ("", ""))
        proc.kill.assert_called_once()


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(
        [GIT_COMMAND, *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


class WorktreeDirtyFilesHardeningTest(unittest.TestCase):
    """`_worktree_dirty_files` runs its `git status` probe through the
    hardened git path, so an agent-planted `core.fsmonitor` in the worktree
    config cannot execute with the orchestrator's process environment. Every
    caller passes an agent-writable worktree, so the probe is hardened
    unconditionally. Real modifications are still reported; only fsmonitor
    execution and the global-config trust boundary are dropped.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-dirty-hardening-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)
        self.work = self.tmpdir / "work"
        self.work.mkdir()
        _run_git("init", QUIET_FLAG, "-b", TEST_BASE_BRANCH, cwd=self.work)
        _run_git(GIT_CONFIG, "user.email", "t@t", cwd=self.work)
        _run_git(GIT_CONFIG, "user.name", "t", cwd=self.work)
        (self.work / SEED_FILE).write_text("x\n")
        _run_git("add", ".", cwd=self.work)
        _run_git("commit", QUIET_FLAG, "-m", SEED_FILE, cwd=self.work)

    def test_blocks_planted_fsmonitor_reports_dirty(self) -> None:
        # Hook + marker live outside the worktree so they are not themselves
        # untracked files. The `/`+NUL response is fsmonitor v1 for "assume
        # everything changed" -- a scan hint only, so a clean tree reads clean.
        marker = self.tmpdir / "fsmonitor_ran.txt"
        hook = self.tmpdir / "fsmonitor_hook.sh"
        hook.write_text(
            f"#!/bin/sh\nprintf ran >> '{marker}'\nprintf '/\\000'\n"
        )
        hook.chmod(EXECUTABLE_MODE)
        _run_git(GIT_CONFIG, "core.fsmonitor", str(hook), cwd=self.work)

        (self.work / LEFTOVER_FILE).write_text("leak\n")
        # Prove the planted hook is genuinely honored: a plain, unhardened
        # index refresh fires it. Without this the empty-marker assertion
        # below could pass simply because the hook was never wired.
        _run_git("status", "--porcelain", cwd=self.work)
        self.assertTrue(
            marker.exists() and marker.read_text(),
            "planted fsmonitor never fired for a plain git status; the test cannot detect a regression",
        )
        marker.unlink()

        dirty = verify._worktree_dirty_files(self.work)

        # The real modification is still reported...
        self.assertIn(LEFTOVER_FILE, dirty)
        # ...but the hardened probe never executed the planted helper with
        # our process environment attached.
        self.assertFalse(
            marker.exists() and marker.read_text(),
            "hardened dirty probe executed the planted core.fsmonitor",
        )
