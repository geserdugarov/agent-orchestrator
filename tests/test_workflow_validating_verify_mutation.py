# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from orchestrator import agents, verify, workflow

from tests import validating_verify_test_support as verify_support

VERIFY_HEAD_CHANGED = "head_changed"
VERIFY_DIRTY = "dirty"
VERIFY_OK = "ok"
PASSING_COMMAND = "true"
LEFTOVER_FILE = "leftover.txt"
GIT_COMMAND = "git"
WORKTREE_FLAG = "-C"


class VerifyCommandMutationTest(
    verify_support.VerifyCommandsFixtureMixin,
    unittest.TestCase,
):
    """Report verify-time commits, dirty output, and process registration."""

    def test_commit_command_reports_head_change(self) -> None:
        # Regression: a verify command that runs `git commit` leaves
        # `git status --porcelain` clean and exits 0, so the previous
        # dirty+exit-code-only gate accepted it as "ok". The squash-on-
        # approval + force-push that followed would then publish the
        # unreviewed verify-created commit to the PR branch. Snapshotting
        # HEAD before the loop and refusing any command that moves it
        # closes that hole.
        head_before = subprocess.run(
            [GIT_COMMAND, WORKTREE_FLAG, str(self.worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Stage and commit a new file inside the verify command itself --
        # exactly the dangerous shape (a verify rule that auto-fixes and
        # commits its own fix).
        cmd = (
            "sh -c 'echo VERIFY_AUTO_FIXED > autofix.txt && "
            "git add autofix.txt && "
            'git commit -q -m "chore: verify-time auto-fix"\''
        )
        run = workflow._run_verify_commands(self.worktree, (cmd,), 60)
        self.assertEqual(run.status, VERIFY_HEAD_CHANGED)
        self.assertEqual(run.command, cmd)
        self.assertEqual(run.head_before, head_before)
        self.assertNotEqual(run.head_after, head_before)
        # And the worktree was clean on detection (not the dirty branch).
        self.assertEqual(run.dirty_files, ())

    def test_dirty_result_keeps_command_output(self) -> None:
        # Regression: previously the dirty check ran once at the end of
        # the loop, so a dirty failure always blamed `commands[-1]` and
        # discarded every command's captured output. The fix checks
        # dirtiness AFTER EACH command so the actual command that left
        # the worktree dirty is named, with its own stdout/stderr
        # preserved for the park comment.
        cmds = (
            PASSING_COMMAND,  # clean, exit 0
            "sh -c 'echo BUILD_LOG_LINE; touch leftover.txt'",  # leaves untracked file
            PASSING_COMMAND,  # should never run
        )
        run = workflow._run_verify_commands(self.worktree, cmds, 60)
        self.assertEqual(run.status, VERIFY_DIRTY)
        # Named command is the SECOND command (the one that left the
        # tree dirty), NOT `commands[-1]`.
        self.assertEqual(run.command, cmds[1])
        self.assertEqual(run.exit_code, 0)
        # The dirty file lands in `dirty_files`.
        self.assertIn(LEFTOVER_FILE, run.dirty_files)
        # The command's stdout is preserved for the park comment so the
        # operator can triage what the command actually did.
        self.assertIn("BUILD_LOG_LINE", run.output)

    def test_running_command_registered_for_shutdown(self) -> None:
        # The shutdown sweep (`agents.terminate_all_running`) only reaches
        # process groups registered in `agents._running_procs`. A verify
        # command must be registered for the lifetime of its run -- otherwise
        # the watchdog's `os._exit` leaves a slow command running and
        # mutating the worktree after the orchestrator has stopped -- and
        # cleared in the `finally` afterward so a finished command does not
        # leak into the registry. Popen is faked so the registry can be
        # inspected mid-run deterministically.
        proc = MagicMock()
        proc.pid = 4242
        proc.returncode = 0
        seen: dict[str, bool] = {}

        proc.communicate.side_effect = verify_support.RegisteredCommunicate(proc, seen)
        with (
            patch.object(verify.subprocess, "Popen", return_value=proc),
            patch.object(verify, "_worktree_dirty_files", return_value=[]),
            patch.object(verify, "_head_sha", return_value="sha"),
        ):
            run = verify._run_verify_commands(self.worktree, (PASSING_COMMAND,), 60)

        self.assertEqual(run.status, VERIFY_OK)
        self.assertTrue(
            seen.get("during"),
            "verify child not registered during the run",
        )
        with agents._running_procs_lock:
            self.assertNotIn(proc, agents._running_procs)
