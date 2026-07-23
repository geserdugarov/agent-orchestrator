# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shlex
import time
import unittest

from orchestrator import workflow

from tests import validating_verify_test_support as verify_support

VERIFY_FAILED = "failed"
VERIFY_TIMEOUT = "timeout"
VERIFY_DIRTY = "dirty"
VERIFY_OK = "ok"
OUTPUT_PAYLOAD_SIZE = 10000
OUTPUT_BUDGET = 4096
PASSING_COMMAND = "true"
LEFTOVER_FILE = "leftover.txt"


class RunVerifyCommandsTest(
    verify_support.VerifyCommandsFixtureMixin,
    unittest.TestCase,
):
    """Run commands, enforce timeouts, and report dirty worktrees."""

    def test_empty_commands_short_circuits_to_ok(self) -> None:
        run = workflow._run_verify_commands(self.worktree, (), 60)
        self.assertEqual(run.status, VERIFY_OK)
        self.assertIsNone(run.command)

    def test_all_commands_pass_returns_ok(self) -> None:
        run = workflow._run_verify_commands(
            self.worktree,
            (PASSING_COMMAND, "echo hello"),
            60,
        )
        self.assertEqual(run.status, VERIFY_OK)

    def test_nonzero_names_first_failed_command(self) -> None:
        run = workflow._run_verify_commands(
            self.worktree,
            (PASSING_COMMAND, "sh -c 'echo boom 1>&2; exit 3'", PASSING_COMMAND),
            60,
        )
        self.assertEqual(run.status, VERIFY_FAILED)
        self.assertEqual(run.command, "sh -c 'echo boom 1>&2; exit 3'")
        self.assertEqual(run.exit_code, 3)
        self.assertIn("boom", run.output)

    def test_timeout_keeps_partial_output(self) -> None:
        # `sleep 5` against a 1s timeout fires `TimeoutExpired`.
        run = workflow._run_verify_commands(
            self.worktree,
            ("sleep 5",),
            timeout=1,
        )
        self.assertEqual(run.status, VERIFY_TIMEOUT)
        self.assertEqual(run.command, "sleep 5")
        self.assertIsNone(run.exit_code)

    def test_timeout_kills_full_process_group(self) -> None:
        # Regression: `subprocess.run(..., shell=True, timeout=...)`
        # only SIGKILLs the shell, leaving its background descendants
        # (`& subshells`, `make -j` workers, pytest-xdist forkers...)
        # alive to keep mutating the worktree after `_run_verify_commands`
        # has already returned `verify_timeout` and the orchestrator has
        # parked the issue. The runner now puts each command in its own
        # process group via `start_new_session=True` and `killpg`s the
        # group on timeout. Verified by having the verify command spawn
        # a background process that would touch a sentinel file AFTER
        # the timeout would have fired -- with the group-kill it never
        # gets to.
        marker = self.worktree / "post_timeout_marker.txt"
        # Background subshell sleeps 2s then touches the marker. Parent
        # shell sleeps 10s so the 1s timeout definitely fires. If the
        # group-kill works, the background subshell dies before its
        # sleep finishes and the marker is never created.
        cmd = f"(sleep 2 && touch {marker}) & sleep 10"
        run = workflow._run_verify_commands(self.worktree, (cmd,), timeout=1)
        self.assertEqual(run.status, VERIFY_TIMEOUT)
        # Wait well past when the background touch would have fired.
        # 3s gives the background its full 2s + 1s of slack.

        time.sleep(3)
        self.assertFalse(
            marker.exists(),
            f"background process survived timeout-kill; {marker} was created",
        )

    def test_dirty_tree_after_success_returns_dirty(self) -> None:
        # Command exits 0 but leaves an untracked file behind.
        run = workflow._run_verify_commands(
            self.worktree,
            ("sh -c 'echo leak > leftover.txt'",),
            60,
        )
        self.assertEqual(run.status, VERIFY_DIRTY)
        self.assertIn(LEFTOVER_FILE, run.dirty_files)

    def test_output_truncated_to_budget(self) -> None:
        padding = "X" * OUTPUT_PAYLOAD_SIZE
        big = f"{padding}TAIL"
        run = workflow._run_verify_commands(
            self.worktree,
            (f"sh -c 'printf %s {shlex.quote(big)}; exit 1'",),
            60,
        )
        self.assertEqual(run.status, VERIFY_FAILED)
        # Tail preserved, leading bulk trimmed.
        self.assertIn("TAIL", run.output)
        self.assertLessEqual(len(run.output), OUTPUT_BUDGET)
