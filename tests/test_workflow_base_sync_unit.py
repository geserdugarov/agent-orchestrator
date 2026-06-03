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
from orchestrator.github import BACKLOG_LABEL, BASE_SYNC_HOLD_LABEL

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakeUser,
    make_issue,
)


class RefreshBaseAndWorktreesUnitTest(unittest.TestCase):
    """Unit-level coverage for the per-tick base refresh helper. Real-git
    integration coverage lives in `RefreshBaseAndWorktreesRealGitTest`
    (`tests/test_workflow_base_sync_real_git.py`).
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-refresh-unit-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)
        self.target_root = self.tmpdir / "target"
        self.target_root.mkdir()
        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=self.target_root,
            base_branch="main",
        )
        self.gh = FakeGitHubClient()

    def test_returns_early_when_base_fetch_fails(self) -> None:
        from unittest.mock import MagicMock
        fetch_fail = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=1, stdout="", stderr="boom",
            )
        )
        sync = MagicMock()
        with patch.object(base_sync, "_authed_target_fetch", fetch_fail), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        sync.assert_not_called()

    def test_returns_early_when_repo_worktrees_root_missing(self) -> None:
        from unittest.mock import MagicMock
        fetch_ok = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )
        )
        sync = MagicMock()
        with patch.object(base_sync, "_authed_target_fetch", fetch_ok), \
             patch.object(
                base_sync, "_repo_worktrees_root",
                return_value=self.tmpdir / "missing",
             ), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        sync.assert_not_called()

    def test_iterates_only_issue_dirs(self) -> None:
        from unittest.mock import MagicMock
        wt_root = self.tmpdir / "worktrees"
        wt_root.mkdir()
        # Two valid issue worktrees, one decompose dir (skipped), one stray
        # file (skipped), one malformed (skipped).
        (wt_root / "issue-7").mkdir()
        (wt_root / "issue-42").mkdir()
        (wt_root / "decompose-7").mkdir()
        (wt_root / "issue-bogus").mkdir()
        (wt_root / "stray.txt").write_text("x")

        fetch_ok = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )
        )
        sync = MagicMock()
        with patch.object(base_sync, "_authed_target_fetch", fetch_ok), \
             patch.object(
                base_sync, "_repo_worktrees_root", return_value=wt_root,
             ), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)

        called_numbers = sorted(c.args[3] for c in sync.call_args_list)
        self.assertEqual(called_numbers, [7, 42])

    def test_per_worktree_exception_is_swallowed(self) -> None:
        from unittest.mock import MagicMock
        wt_root = self.tmpdir / "worktrees"
        wt_root.mkdir()
        (wt_root / "issue-1").mkdir()
        (wt_root / "issue-2").mkdir()
        fetch_ok = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )
        )
        sync = MagicMock(side_effect=[RuntimeError("kaboom"), None])
        with patch.object(base_sync, "_authed_target_fetch", fetch_ok), \
             patch.object(
                base_sync, "_repo_worktrees_root", return_value=wt_root,
             ), \
             patch.object(base_sync, "_sync_worktree_with_base", sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        # Both worktrees attempted despite the first raising.
        self.assertEqual(sync.call_count, 2)

    def test_base_fetch_uses_per_spec_authed_helper(self) -> None:
        # The base refresh must go through `_authed_target_fetch` (which
        # resolves the per-spec token and uses the spec's `remote_name`
        # for refs/remotes/<remote_name>/<branch>), NOT plain
        # `_git("fetch", ...)`. Without this, a multi-remote spec where
        # `remote_name != origin` falls back to the ambient git
        # credential helper -- which fails under systemd with
        # `terminal prompts disabled`.
        private_spec = config.RepoSpec(
            slug="acme/widget-private",
            target_root=self.target_root,
            base_branch="cache-main",
            remote_name="private",
        )
        fetch_calls: list[tuple] = []

        def fake_fetch(spec, branch):
            fetch_calls.append((spec, branch))
            return subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )

        # Block any plain-git fetch to assert it never runs.
        plain_git_calls: list[tuple] = []

        def fake_git(*args, cwd):
            plain_git_calls.append(args)
            return subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr="",
            )

        with patch.object(base_sync, "_authed_target_fetch", side_effect=fake_fetch), \
             patch.object(base_sync, "_git", side_effect=fake_git), \
             patch.object(
                base_sync, "_repo_worktrees_root",
                return_value=self.tmpdir / "missing",
             ):
            workflow._refresh_base_and_worktrees(self.gh, private_spec)

        self.assertEqual(
            fetch_calls, [(private_spec, "cache-main")],
            "base refresh must route through `_authed_target_fetch` with "
            "the spec's base branch",
        )
        # No plain-git fetch was issued -- otherwise the multi-remote
        # token-selection regression resurfaces.
        for args in plain_git_calls:
            self.assertNotEqual(
                args[0] if args else "", "fetch",
                f"plain `_git(\"fetch\", ...)` leaked: {args!r}",
            )


class SyncWorktreeWithBaseUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/refresh-target"),
            base_branch="main",
        )
        self.wt = Path("/tmp/refresh-wt")
        self.gh = FakeGitHubClient()
        self.gh.add_issue(make_issue(7, label="implementing"))

    def _git_result(self, *, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git"], returncode=returncode, stdout=stdout, stderr="",
        )

    def _add_pr(
        self,
        *,
        pr_number: int = 42,
        head_branch: str = "orchestrator/issue-7",
        merged: bool = False,
        state: str = "open",
    ) -> FakePR:
        pr = FakePR(
            number=pr_number, head_branch=head_branch,
            merged=merged, state=state,
        )
        self.gh.add_pr(pr)
        return pr

    def test_pr_having_in_review_behind_routes_to_resolving_conflict(
        self,
    ) -> None:
        # A local-only base update on a worktree whose branch has already
        # been pushed diverges local HEAD from `pr.head.sha` and breaks
        # `_squash_and_force_push`'s `--force-with-lease=<original_head>`
        # check (the remote is still at the un-merged tip). The fix is to
        # detour the issue to `resolving_conflict` so the existing handler
        # does rebase + push + relabel-to-validating in one consistent flow.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        merge = MagicMock()
        # Behind base by 3 commits.
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Local base update MUST NOT have happened on the PR worktree.
        merge.assert_not_called()
        # Label flipped to resolving_conflict.
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)
        # PR comment posted.
        self.assertEqual(len(self.gh.posted_pr_comments), 1)
        self.assertEqual(self.gh.posted_pr_comments[0][0], 42)
        self.assertIn("auto-resolution", self.gh.posted_pr_comments[0][1])
        # `conflict_round` initialized to 0 (the cap counter).
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("conflict_round"), 0)

    def test_pr_having_validating_behind_also_routes(self) -> None:
        # Validating is a long-lived label too (the reviewer hasn't approved
        # yet). The detour fires here so the reviewer doesn't run on a
        # stale-base local HEAD.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="validating"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)

    def test_pr_having_documenting_behind_also_routes(self) -> None:
        # `documenting` is the brief final-docs hop between reviewer
        # approval and `in_review`. The handler only checks ahead/behind
        # against the PR branch, not the base, so a sibling-PR merge
        # during the docs pass must be caught by the pre-tick detour --
        # otherwise the docs commit would land on a stale base and only
        # the next in_review tick would auto-rebase it. `hold_base_sync`
        # must remain the only label that gates this auto-rebase.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="documenting"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)

    def test_hold_base_sync_label_skips_pr_refresh_detour(self) -> None:
        from unittest.mock import MagicMock
        issue = make_issue(7, label="in_review")
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))
        self.gh.add_issue(issue)
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_hold_base_sync_label_skips_pre_pr_base_rebase(self) -> None:
        from unittest.mock import MagicMock
        issue = make_issue(7, label="implementing")
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))
        self.gh.add_issue(issue)
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])

    def test_backlog_label_skips_pr_refresh_detour(self) -> None:
        # `backlog` is a hard skip: the refresh path must not relabel the
        # issue to `resolving_conflict` or post a PR notice while the
        # operator has the issue postponed.
        from unittest.mock import MagicMock
        issue = make_issue(7, label="in_review")
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        self.gh.add_issue(issue)
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_backlog_label_skips_pre_pr_base_rebase(self) -> None:
        from unittest.mock import MagicMock
        issue = make_issue(7, label="implementing")
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        self.gh.add_issue(issue)
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        merge.assert_not_called()
        self.assertEqual(self.gh.label_history, [])

    def test_pr_having_resolving_conflict_label_does_not_re_route(self) -> None:
        # The handler runs this tick anyway and will do the rebase -- a
        # second label flip is pointless and would re-post the PR notice.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="resolving_conflict"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No new label flip (the issue was already labeled
        # `resolving_conflict` at fixture time, not by us).
        self.assertEqual(self.gh.label_history, [])
        # No duplicate PR notice.
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_having_up_to_date_does_not_route(self) -> None:
        # behind = 0 short-circuits: nothing to refresh, no detour.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_preserves_existing_conflict_round(self) -> None:
        # On re-entry from a previous resolving_conflict round, the cap
        # counter must NOT reset to 0 -- mirrors `_handle_in_review`'s
        # "set when absent" semantics so a perpetually-stuck PR can't
        # ping-pong forever.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/issue-7", conflict_round=2,
        )
        self._add_pr()
        git_mock = MagicMock(return_value=self._git_result(stdout="1\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("conflict_round"), 2)

    def test_pr_route_skips_merged_pr(self) -> None:
        # Regression: a just-merged PR advances `origin/<base>`, so the
        # still-in_review worktree pointed at the now-stale branch is
        # naturally behind. Without the PR-state gate the refresh would
        # post an "auto-resolution" notice and relabel the issue to
        # `resolving_conflict` on a PR the next handler call would
        # finalize to `done`. Leaving the label alone lets the existing
        # in_review terminal handler (or the closed-issue sweep variant)
        # do its job.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr(merged=True, state="closed")
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_skips_closed_unmerged_pr(self) -> None:
        # Same regression for the rejected terminal: a closed-without-merge
        # PR that happens to be behind base must not be relabeled to
        # `resolving_conflict`. The handler will finalize to `rejected`.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        self._add_pr(merged=False, state="closed")
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_skips_when_get_pr_fails(self) -> None:
        # Defensive: if PR state cannot be determined this tick, leave the
        # label alone -- the handler can retry from a stable label rather
        # than racing a half-known state.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(7, pr_number=42, branch="orchestrator/issue-7")
        # No PR added -- get_pr will raise KeyError on the FakeGitHubClient.
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        self.assertEqual(self.gh.label_history, [])
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_pr_route_does_not_bump_in_review_watermark(self) -> None:
        # Regression: the refresh-time detour runs BEFORE any handler scans
        # comments. Bumping `pr_last_comment_id` past `latest_comment_id`
        # would silently mark unread human "do not merge" / fix-request
        # comments as consumed; the next `_handle_in_review` scan would
        # then skip them and the in_review HITL ready-ping could
        # advertise the PR as ready for human merge over the human
        # signal. The watermark must be left alone here -- the next
        # in_review scan will pick the human comments up correctly, and
        # the orchestrator's own PR notice is filtered via
        # `orchestrator_comment_ids` so it does not replay either.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/issue-7",
            pr_last_comment_id=100,
        )
        self._add_pr()
        # An UNREAD human comment landed AFTER the current watermark of 100.
        # If we bump the watermark to `latest_comment_id` (max id seen, which
        # would include this human comment), it gets silently consumed.
        self.gh._issues[7].comments.append(FakeComment(
            id=500, body="do not merge yet", user=FakeUser("human"),
        ))
        git_mock = MagicMock(return_value=self._git_result(stdout="1\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        data = self.gh.pinned_data(7)
        # Watermark stayed at 100 -- the unread human comment at id=500 is
        # still ahead of it and the next in_review scan will pick it up.
        self.assertEqual(data.get("pr_last_comment_id"), 100)

    def test_pr_route_skips_when_awaiting_human(self) -> None:
        # Regression: a parked PR (`awaiting_human=True`) must not be
        # detoured. `_handle_resolving_conflict`'s awaiting-human branch
        # returns early without rebasing unless a new human comment arrives,
        # so relabeling here would silently hide the existing park behind a
        # `resolving_conflict` label without making any progress -- including
        # the documented `in_review` unmergeable park path. Leaving the
        # park intact preserves its visibility and the human-driven recovery
        # path the park already invited.
        from unittest.mock import MagicMock
        self.gh.add_issue(make_issue(7, label="in_review"))
        self.gh.seed_state(
            7, pr_number=42, branch="orchestrator/issue-7",
            awaiting_human=True, park_reason="unmergeable",
        )
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_git", git_mock):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # No relabel: park left intact.
        self.assertEqual(self.gh.label_history, [])
        # No PR notice posted (would have been duplicate noise on a parked
        # issue that already has an HITL ping).
        self.assertEqual(self.gh.posted_pr_comments, [])

    def test_skips_dirty_worktree(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock()
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=["a.py"],
        ), patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()

    def test_skips_when_already_up_to_date(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(stdout="0\n"))
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()

    def test_skips_when_rev_list_fails(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock()
        git_mock = MagicMock(return_value=self._git_result(returncode=128))
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_not_called()

    def test_clean_rebase_when_behind(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock(return_value=(True, []))
        git_mock = MagicMock(return_value=self._git_result(stdout="3\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        merge.assert_called_once()
        # No abort issued on success.
        self.assertFalse(
            any(c.args[:1] == ("rebase",) for c in hardened.call_args_list)
        )

    def test_conflict_aborts_and_swallows(self) -> None:
        from unittest.mock import MagicMock
        merge = MagicMock(return_value=(False, ["a.py", "b.py"]))
        git_mock = MagicMock(return_value=self._git_result(stdout="2\n"))
        hardened = MagicMock(return_value=self._git_result())
        with patch.object(
            base_sync, "_worktree_dirty_files", return_value=[],
        ), patch.object(base_sync, "_git", git_mock), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_git_hardened", hardened):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)
        # Abort issued exactly once.
        abort_calls = [
            c for c in hardened.call_args_list
            if c.args[:2] == ("rebase", "--abort")
        ]
        self.assertEqual(len(abort_calls), 1)

    def test_missing_issue_is_swallowed(self) -> None:
        # An orphan worktree (issue deleted on GitHub side, or fetch error)
        # must not crash the refresh -- skip silently.
        from unittest.mock import MagicMock
        merge = MagicMock()
        with patch.object(base_sync, "_rebase_base_into_worktree", merge):
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 9999)
        merge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
