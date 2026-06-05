# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_pr_open_clean_base_advance_rebases_and_routes_to_validating(
        self,
    ) -> None:
        # The #402-style case: an open PR branch is merely behind base
        # (no content conflicts). The refresh must rebase the worktree
        # onto the new base, push the rewritten branch with
        # force-with-lease pinned to the pre-rebase SHA, reset
        # `review_round`, and relabel to `validating` -- NOT to
        # `resolving_conflict`. `resolving_conflict` is reserved for
        # rebases that actually leave conflicted files.
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/issue-7", review_round=4,
        )
        self.gh.add_pr(FakePR(
            number=42, head_branch="orchestrator/issue-7",
            merged=False, state="open",
        ))
        # Publish the orchestrator branch to the bare remote so the
        # force-with-lease check has a known SHA to compare against
        # (the production PR flow does the same first push when
        # `_handle_implementing` opens the PR).
        self._git("push", "origin", "orchestrator/issue-7", cwd=self.wt)
        self._advance_base(conflicting=False)
        head_before = self._wt_head()

        # Stub `_push_branch` so the real git push (which would dial out
        # to a non-existent remote) is replaced with a local push to the
        # bare remote we set up in setUp -- and so we can verify the
        # force-with-lease value the caller pinned. The signature mirrors
        # the production helper.
        captured: dict[str, str] = {}

        def fake_push(spec, worktree, branch, *, force_with_lease=None):
            captured["branch"] = branch
            captured["force_with_lease"] = force_with_lease or ""
            r = subprocess.run(
                ["git", "push",
                 f"--force-with-lease=refs/heads/{branch}:{force_with_lease or ''}",
                 "origin", f"HEAD:refs/heads/{branch}"],
                cwd=str(worktree),
                capture_output=True, text=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            return r.returncode == 0

        with patch.object(base_sync, "_push_branch", side_effect=fake_push), \
             patch.object(
                workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
             ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)

        # The local HEAD moved: the rebase replayed the feature commit
        # onto the new base, then the push delivered the rewrite.
        self.assertNotEqual(head_before, self._wt_head())
        # The base file landed in the worktree -- the rebase result.
        self.assertTrue((self.wt / "extra.txt").exists())
        # Worktree is clean.
        self.assertTrue(self._is_clean())
        # The push was issued with force-with-lease pinned to the
        # pre-rebase SHA (= the remote PR head at the time).
        self.assertEqual(captured.get("branch"), "orchestrator/issue-7")
        self.assertEqual(captured.get("force_with_lease"), head_before)
        # Label flipped to `validating`, NOT `resolving_conflict`.
        self.assertIn((7, "validating"), self.gh.label_history)
        self.assertNotIn((7, "resolving_conflict"), self.gh.label_history)
        # `review_round` reset so the reviewer re-runs against the new head.
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("review_round"), 0)
        # No `conflict_round` seeded -- this was not a conflict path.
        self.assertIsNone(data.get("conflict_round"))

    def test_pr_open_clean_rebase_push_failure_resets_local_head(self) -> None:
        # Regression for issue #413 review: a clean local rebase whose
        # push fails (lease rejection on a diverged remote, transient
        # network error, etc.) must NOT leave local HEAD on the
        # rebased SHA. If we did, the next tick's behind check (HEAD vs
        # `origin/<base>`) would report `behind == 0` and never retry,
        # and `validating` would review a local HEAD that is NOT on
        # the PR. The recovery path resets HEAD back to the pre-rebase
        # SHA so the worktree matches the still-stale remote PR head
        # and the next refresh tick picks the work up again.
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self.gh.add_pr(FakePR(
            number=42, head_branch="orchestrator/issue-7",
            merged=False, state="open",
        ))
        # Publish the branch so the lease has a real SHA to compare
        # against, then advance base cleanly.
        self._git("push", "origin", "orchestrator/issue-7", cwd=self.wt)
        self._advance_base(conflicting=False)
        head_before = self._wt_head()

        # Stub `_push_branch` to simulate the lease rejection: return
        # False without touching the bare remote (the production lease
        # check would have done the same thing on a diverged remote).
        push = MagicMock(return_value=False)

        with patch.object(base_sync, "_push_branch", push), \
             patch.object(
                workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
             ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)

        # Push was attempted exactly once.
        push.assert_called_once()
        # Local HEAD is back at the pre-rebase SHA (= the still-stale
        # remote PR head), NOT the rebased SHA the failed push would
        # have published.
        self.assertEqual(head_before, self._wt_head())
        # The base file did NOT land in the worktree -- the reset
        # restored the tree to its pre-rebase state.
        self.assertFalse((self.wt / "extra.txt").exists())
        # Worktree is clean.
        self.assertTrue(self._is_clean())
        # Label stays put; no relabel to `validating` or
        # `resolving_conflict`.
        self.assertEqual(self.gh.label_history, [])
        # No PR notice posted -- the recovery is silent so a
        # transient push failure does not spam the PR thread.
        self.assertEqual(self.gh.posted_pr_comments, [])
        # `review_round` was NOT reset since we did not flip the label.
        data = self.gh.pinned_data(7)
        self.assertIsNone(data.get("review_round"))

    def test_pr_open_conflicting_base_advance_relabels_resolving_conflict(
        self,
    ) -> None:
        # When the rebase actually leaves conflicted files, the refresh
        # DOES relabel to `resolving_conflict` so `_handle_resolving_conflict`
        # can drive the dev agent to resolve them. This is the only path
        # that still enters `resolving_conflict` from the refresh.
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self.gh.add_pr(FakePR(
            number=42, head_branch="orchestrator/issue-7",
            merged=False, state="open",
        ))
        self._advance_base(conflicting=True)
        head_before = self._wt_head()

        push = MagicMock()
        with patch.object(base_sync, "_push_branch", push), \
             patch.object(
                workflow.config, "WORKTREES_DIR", self.tmpdir / "worktrees",
             ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)

        # The rebase was attempted and aborted on conflict -- HEAD stays.
        self.assertEqual(head_before, self._wt_head())
        # Worktree clean (the abort restored it).
        self.assertTrue(self._is_clean())
        # No push was issued -- the dev agent will resolve the conflict.
        push.assert_not_called()
        # Label flipped to `resolving_conflict`.
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)
        # `conflict_round` initialized to 0.
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("conflict_round"), 0)


if __name__ == "__main__":
    unittest.main()
