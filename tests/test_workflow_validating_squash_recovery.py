# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests import validating_squash_test_support as squash_support

EXECUTABLE_MODE = 0o755
GIT_LOG = "log"
LAST_COMMIT = "-1"
SCRATCH_FILE = "scratch.txt"


class SquashHelperRecoveryRealGitTest(
    squash_support.SquashGitFixtureMixin,
    unittest.TestCase,
):
    """Preserve branches and worktrees across no-op and failure paths."""

    def test_squash_with_only_one_commit_is_a_no_op(self) -> None:
        # Reset to a single commit on top of base.
        self._rebuild_single_commit()
        original_head = self._head_sha()

        squash_run = self._squash()
        self.assertTrue(squash_run.success)
        self.assertEqual(squash_run.count, 0)
        self.assertEqual(squash_run.sha, original_head)
        # Single-commit branch must NOT trigger a push at all.
        squash_run.push_mock.assert_not_called()
        # HEAD unchanged.
        self.assertEqual(self._head_sha(), original_head)

    def test_push_failure_rollback_restores_branch(self) -> None:
        # The whole point of saving original_head: a push failure after
        # the soft-reset + squash commit must not leave the branch
        # pointing at the squash commit. The original commits must still
        # be on the branch so the operator can decide what to do.
        original_head = self._head_sha()
        original_subjects = self._commits_on_branch()
        self.assertEqual(len(original_subjects), 3)

        squash_run = self._squash(push_result=False)
        self.assertFalse(squash_run.success)
        self.assertIsNone(squash_run.sha)
        self.assertEqual(squash_run.count, 0)
        self.assertIn("force-push", squash_run.error or "")
        # HEAD restored.
        self.assertEqual(
            self._head_sha(),
            original_head,
            "rollback must restore HEAD to the pre-squash SHA",
        )
        # All three original commits still on the branch.
        self.assertEqual(self._commits_on_branch(), original_subjects)
        # Working tree clean (rollback used --hard, but pre-reset tree
        # already matched HEAD's tree, so no file diffs should remain).
        status = squash_support.run_git("status", "--porcelain", cwd=self.work)
        self.assertEqual(status.strip(), "")

    def test_never_executes_planted_fsmonitor(self) -> None:
        # Every index-refreshing git command in the squash helper -- the
        # pre-rewrite dirty check, the soft reset, the squash commit, and the
        # post-push rollback `reset --hard` -- runs inside a worktree whose
        # `.git/config` the agent can write. A planted `core.fsmonitor` helper
        # would run during any of them with the orchestrator's process
        # environment (ambient secrets) attached, so each must go through the
        # hardened git path that disables fsmonitor. This drives the whole
        # helper to the rollback branch (push mocked to fail) and asserts the
        # planted hook fired NOWHERE inside it -- while first proving the hook
        # is genuinely usable, so the negative assertion is not vacuous.
        marker = self._install_fsmonitor()
        original_head = self._head_sha()
        original_subjects = self._commits_on_branch()
        self.assertEqual(len(original_subjects), 3)

        squash_run = self._squash(push_result=False)

        fired = marker.read_text() if marker.exists() else ""
        # The security property: no git command inside the squash helper
        # executed the planted fsmonitor. A plain `_git` dirty check / reset
        # would appear here with the orchestrator environment attached.
        self.assertEqual(
            fired,
            "",
            f"a git command inside the squash helper executed the planted fsmonitor: {fired!r}",
        )
        # Push failed, so the rollback ran and restored the original commits.
        self.assertFalse(squash_run.success)
        self.assertIn("force-push", squash_run.error or "")
        self.assertEqual(
            self._head_sha(),
            original_head,
            "rollback must restore HEAD to the pre-squash SHA",
        )
        self.assertEqual(self._commits_on_branch(), original_subjects)

    def test_squash_commit_uses_orchestrator_identity(self) -> None:
        # The squash commit must be authored under AGENT_GIT_NAME /
        # AGENT_GIT_EMAIL regardless of the dev's commit identity. This
        # keeps a single attribution for orchestrator-owned commits and
        # matches the agent-spawn `_agent_env` behavior.
        squash_run = self._squash(
            AGENT_GIT_NAME="orch-bot",
            AGENT_GIT_EMAIL="orch-bot@example.com",
        )
        self.assertTrue(squash_run.success, squash_run.error)

        author = squash_support.run_git(
            GIT_LOG,
            LAST_COMMIT,
            "--pretty=%an <%ae>",
            cwd=self.work,
        ).strip()
        committer = squash_support.run_git(
            GIT_LOG,
            LAST_COMMIT,
            "--pretty=%cn <%ce>",
            cwd=self.work,
        ).strip()
        self.assertEqual(author, "orch-bot <orch-bot@example.com>")
        self.assertEqual(committer, "orch-bot <orch-bot@example.com>")

    def test_dirty_worktree_aborts_before_reset(self) -> None:
        # An uncommitted change in the worktree (the agent left work
        # behind) is a refuse-to-rewrite signal: the helper must abort
        # WITHOUT touching HEAD so the dirty state is visible to the
        # operator. Without the pre-reset dirty check the soft-reset
        # would happen and the rollback would clobber the dirty changes.
        original_head = self._head_sha()
        (self.work / SCRATCH_FILE).write_text("uncommitted\n")

        squash_run = self._squash()
        self.assertFalse(squash_run.success)
        self.assertIn("uncommitted", squash_run.error or "")
        # HEAD untouched, dirty file preserved, no push attempted.
        self.assertEqual(self._head_sha(), original_head)
        self.assertTrue((self.work / SCRATCH_FILE).exists())
        squash_run.push_mock.assert_not_called()

    def test_dirty_single_commit_still_fails(self) -> None:
        # The dirty-tree refusal is a precondition for the whole helper,
        # not just the rewrite path. A one-commit branch (squash would
        # be a no-op) with an uncommitted file must still fail so the
        # caller parks awaiting_human; otherwise the manual merge could
        # land the head with the operator's scratch invisible on the PR.
        self._rebuild_single_commit()
        original_head = self._head_sha()
        (self.work / SCRATCH_FILE).write_text("uncommitted\n")

        squash_run = self._squash()
        self.assertFalse(squash_run.success)
        self.assertIsNone(squash_run.sha)
        self.assertEqual(squash_run.count, 0)
        self.assertIn("uncommitted", squash_run.error or "")
        # Single-commit + dirty path must NOT short-circuit to the
        # no-op success branch. HEAD untouched, dirty file preserved,
        # no push attempted.
        self.assertEqual(self._head_sha(), original_head)
        self.assertTrue((self.work / SCRATCH_FILE).exists())
        squash_run.push_mock.assert_not_called()

    def _install_fsmonitor(self):
        marker = self.tmpdir / "fsmonitor_invocations.txt"
        hook = self.tmpdir / "fsmonitor_hook.sh"
        hook_lines = (
            "#!/bin/sh",
            rf"tr '\0' ' ' < /proc/$PPID/cmdline >> '{marker}'",
            rf"printf '\n' >> '{marker}'",
            r"printf '/\000'",
        )
        hook.write_text("\n".join((*hook_lines, "")))
        hook.chmod(EXECUTABLE_MODE)
        squash_support.run_git(
            "config",
            "core.fsmonitor",
            str(hook),
            cwd=self.work,
        )
        squash_support.run_git("status", "--porcelain", cwd=self.work)
        self.assertTrue(
            marker.exists() and marker.read_text().strip(),
            "planted fsmonitor did not run for a plain git status",
        )
        marker.unlink()
        return marker
