# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import base_sync, config, workflow

from tests.fakes import FakeGitHubClient, FakePR, make_issue


class RefreshBaseAndWorktreesRealGitTest(unittest.TestCase):
    """Integration coverage for `_refresh_base_and_worktrees` against a real
    bare remote + per-issue worktree. Mirrors `SquashHelperRealGitTest`'s
    setup so the helper's interaction with `git fetch` / `git rebase` /
    `git rebase --abort` is exercised end-to-end.
    """

    def _git(self, *args: str, cwd: Path, env_extra: dict | None = None) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if env_extra:
            env.update(env_extra)
        r = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, env=env, check=True,
        )
        return r.stdout

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-refresh-real-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)

        self.remote = self.tmpdir / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.remote)],
            check=True, capture_output=True,
        )
        self.work = self.tmpdir / "work"
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.work)],
            check=True, capture_output=True,
        )
        author_env = {
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
        }
        self._author_env = author_env
        (self.work / "README.md").write_text("hello\n")
        self._git("add", ".", cwd=self.work)
        self._git("commit", "-m", "initial", cwd=self.work, env_extra=author_env)
        self._git("push", "origin", "main", cwd=self.work)

        # Per-issue worktree branched off origin/main, with one local commit.
        self.wt_root = self.tmpdir / "worktrees" / "acme__widget"
        self.wt_root.mkdir(parents=True)
        self.wt = self.wt_root / "issue-7"
        self._git(
            "worktree", "add", "-b", "orchestrator/issue-7",
            str(self.wt), "origin/main", cwd=self.work,
        )
        (self.wt / "feature.py").write_text("feature\n")
        self._git("add", ".", cwd=self.wt)
        self._git(
            "commit", "-m", "feat: add feature", cwd=self.wt,
            env_extra=author_env,
        )

        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=self.work,
            base_branch="main",
        )
        # Default: per-issue worktree #7 is in `implementing` (no PR yet),
        # so the refresh is allowed to rebase it onto base. Tests that want
        # the PR-skip path call `_seed_pr_state(7)`.
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(7, label="implementing"))

        # `_authed_target_fetch` would otherwise dial out to
        # `https://x-access-token@github.com/acme/widget.git`, which
        # does not exist for our local bare remote. Redirect it to a
        # plain `git fetch <remote_name> <branch>` against the
        # local-clone `origin` so the integration test still exercises
        # the post-fetch merge / refresh logic end-to-end.
        def _local_fetch(spec, branch):
            r = subprocess.run(
                ["git", "fetch", "--quiet", spec.remote_name, branch],
                cwd=str(spec.target_root),
                capture_output=True, text=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            return r

        self._fetch_patch = patch.object(
            base_sync, "_authed_target_fetch", side_effect=_local_fetch,
        )
        self._fetch_patch.start()
        self.addCleanup(self._fetch_patch.stop)

    def _seed_pr_state(
        self, issue_number: int, pr_number: int = 999, *,
        merged: bool = False, state: str = "open",
    ) -> None:
        self.gh.seed_state(
            issue_number, pr_number=pr_number,
            branch=f"orchestrator/issue-{issue_number}",
        )
        self.gh.add_pr(FakePR(
            number=pr_number,
            head_branch=f"orchestrator/issue-{issue_number}",
            merged=merged, state=state,
        ))

    def _advance_base(self, *, conflicting: bool) -> None:
        """Push a new commit to origin/main. When `conflicting=True`, the
        commit edits `feature.py` so a base rebase of the per-issue branch
        will conflict with the local feature commit.
        """
        self._git("checkout", "main", cwd=self.work)
        path = self.work / ("feature.py" if conflicting else "extra.txt")
        path.write_text("base side\n")
        self._git("add", ".", cwd=self.work)
        self._git(
            "commit", "-m", "base advance", cwd=self.work,
            env_extra=self._author_env,
        )
        self._git("push", "origin", "main", cwd=self.work)

    def _wt_head(self) -> str:
        return self._git("rev-parse", "HEAD", cwd=self.wt).strip()

    def _is_clean(self) -> bool:
        return self._git("status", "--porcelain", cwd=self.wt).strip() == ""

    def test_clean_advance_rebases_worktree(self) -> None:
        self._advance_base(conflicting=False)
        head_before = self._wt_head()
        with patch.object(
            workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        head_after = self._wt_head()
        self.assertNotEqual(head_before, head_after)
        # The base file landed in the worktree's tree.
        self.assertTrue((self.wt / "extra.txt").exists())
        self.assertEqual(
            self._git("log", "-1", "--format=%s", cwd=self.wt).strip(),
            "feat: add feature",
        )
        self.assertTrue(self._is_clean())

    def test_no_op_when_already_up_to_date(self) -> None:
        head_before = self._wt_head()
        with patch.object(
            workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        self.assertEqual(head_before, self._wt_head())
        self.assertTrue(self._is_clean())

    def test_conflict_aborts_leaving_worktree_clean(self) -> None:
        self._advance_base(conflicting=True)
        head_before = self._wt_head()
        with patch.object(
            workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        # HEAD did NOT move (rebase aborted) and worktree is clean again --
        # the conflict surfaces later via the resolving_conflict stage.
        self.assertEqual(head_before, self._wt_head())
        self.assertTrue(self._is_clean())

    def test_dirty_worktree_skipped_without_disturbing_changes(self) -> None:
        self._advance_base(conflicting=False)
        # Plant an uncommitted edit in the worktree -- mirrors a mid-flight
        # agent edit. The base rebase must NOT run.
        (self.wt / "scratch.py").write_text("scratch\n")
        head_before = self._wt_head()
        with patch.object(
            workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        self.assertEqual(head_before, self._wt_head())
        # Untracked file still present, nothing else was added.
        self.assertTrue((self.wt / "scratch.py").exists())
        self.assertFalse((self.wt / "extra.txt").exists())

    def test_pr_open_worktree_is_not_merged_locally(self) -> None:
        # Regression: once a PR exists, the per-issue branch has been pushed
        # and `pr.head.sha` equals local HEAD. A local-only base rebase would
        # diverge them and break the validating reviewer (it reads local
        # HEAD) and `_squash_and_force_push`'s lease check (it expects the
        # remote to equal `original_head` = local HEAD). The refresh must
        # NOT do a local rebase here; instead it routes the issue to
        # `resolving_conflict` so the existing handler does rebase + push +
        # relabel-to-validating in one consistent flow.
        # Replace the default `implementing` issue with one in `in_review`
        # plus the PR-having pinned state.
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(7, label="in_review"))
        self._seed_pr_state(7, pr_number=42)
        self._advance_base(conflicting=False)
        head_before = self._wt_head()
        with patch.object(
            workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        # HEAD did NOT move: no local-only rebase was performed.
        self.assertEqual(head_before, self._wt_head())
        # The base file did NOT land in the worktree (not yet -- it will
        # after `_handle_resolving_conflict` runs and pushes).
        self.assertFalse((self.wt / "extra.txt").exists())
        # But the issue WAS routed to resolving_conflict so the handler
        # picks it up.
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)



if __name__ == "__main__":
    unittest.main()
