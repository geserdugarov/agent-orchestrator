# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

from orchestrator import config, workflow

from tests.fakes import (
    FakeGitHubClient,
)

# --- Shared base-sync fixture literals -----------------------------------
# One worktree per issue drives every scenario here: issue #7 with an open
# PR #42 on the canonical head branch of the `acme/widget` target repo.
from tests.base_sync_test_support import (
    _git_result,
    _patch_base_sync,
)

ISSUE = 7
PR_NUMBER = 42
SLUG = "acme/widget"
BASE_BRANCH = "main"
PR_BRANCH = "orchestrator/acme__widget/issue-7"

# Multi-remote spec exercised by the per-spec authed-fetch regression.
PRIVATE_SLUG = "acme/widget-private"
PRIVATE_BASE_BRANCH = "cache-main"
PRIVATE_REMOTE = "private"

# Worktree HEAD SHAs threaded through the rebase / push / recovery flows.
BEFORE_SHA = "before-sha"
AFTER_SHA = "after-sha"
REBASED_SHA = "rebased-sha"
# Remote PR head planted so the conflict-round event can assert its `sha`.
CONFLICT_PR_HEAD_SHA = "cafef00dcafef00d"

# Workflow labels the refresh routes between.
LABEL_IN_REVIEW = "in_review"
LABEL_VALIDATING = "validating"
LABEL_RESOLVING_CONFLICT = "resolving_conflict"
LABEL_DOCUMENTING = "documenting"
LABEL_IMPLEMENTING = "implementing"

# Audit event names emitted by the base-sync flow.
EVENT_BASE_REBASED = "base_rebased"
EVENT_CONFLICT_ROUND = "conflict_round"

# Awaiting-human park reasons the auto-rebase flow writes.
PARK_PUSH_FAILED = "auto_base_rebase_push_failed"
PARK_DIRTY = "auto_base_rebase_dirty"
PARK_FAILED = "auto_base_rebase_failed"

# Pinned-state field keys read back from `gh.pinned_data(...)`.
KEY_AWAITING_HUMAN = "awaiting_human"
KEY_PARK_REASON = "park_reason"
KEY_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"
KEY_REVIEW_ROUND = "review_round"
KEY_CONFLICT_ROUND = "conflict_round"
KEY_LAST_ACTION_COMMENT_ID = "last_action_comment_id"
KEY_PR_LAST_COMMENT_ID = "pr_last_comment_id"

# Git output, command, and event fields shared by the scenario assertions.
THREE_BEHIND_STDOUT = "3\n"
TWO_BEHIND_STDOUT = "2\n"
UP_TO_DATE_STDOUT = "0\n"
REBASE_COMMAND = "rebase"
ABORT_FLAG = "--abort"
RESET_COMMAND = "reset"
HARD_RESET_FLAG = "--hard"
FORCE_WITH_LEASE_KWARG = "force_with_lease"
EVENT_FIELD = "event"
SHA_FIELD = "sha"
METHOD_FIELD = "method"

# Stable identities and values used across park and recovery scenarios.
HUMAN_LOGIN = "human"
PARK_WATERMARK_COMMENT_ID = 99
RETRY_COMMENT_ID = 200
OUTSIDER_COMMENT_ID = 201
UNREAD_COMMENT_ID = 500
GIT_FAILURE_EXIT_CODE = 128
MISSING_ISSUE_NUMBER = 9999
NEW_REBASED_SHA = "new-rebased-sha"


class RefreshBaseAndWorktreesUnitTest(unittest.TestCase):
    """Unit-level coverage for the per-tick base refresh helper. Real-git
    integration coverage lives in the focused
    ``test_workflow_base_sync_real_git*`` modules.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="orch-refresh-unit-"))
        self.addCleanup(shutil.rmtree, str(self.tmpdir), ignore_errors=True)
        self.target_root = self.tmpdir / "target"
        self.target_root.mkdir()
        self.spec = config.RepoSpec(
            slug=SLUG,
            target_root=self.target_root,
            base_branch=BASE_BRANCH,
        )
        self.gh = FakeGitHubClient()

    def test_returns_early_when_base_fetch_fails(self) -> None:
        fetch_fail = MagicMock(return_value=_git_result(returncode=1, stderr="boom"))
        sync = MagicMock()
        with _patch_base_sync(target_fetch=fetch_fail, sync=sync):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        sync.assert_not_called()

    def test_returns_early_without_worktree_root(self) -> None:
        fetch_ok = MagicMock(return_value=_git_result())
        sync = MagicMock()
        with _patch_base_sync(
            target_fetch=fetch_ok,
            worktrees_root=MagicMock(return_value=self.tmpdir / "missing"),
            sync=sync,
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)
        sync.assert_not_called()

    def test_iterates_only_issue_dirs(self) -> None:
        wt_root = self.tmpdir / "worktrees"
        wt_root.mkdir()
        # Two valid issue worktrees, one decompose dir (skipped), one stray
        # file (skipped), one malformed (skipped).
        (wt_root / "issue-7").mkdir()
        (wt_root / "issue-42").mkdir()
        (wt_root / "decompose-7").mkdir()
        (wt_root / "issue-bogus").mkdir()
        (wt_root / "stray.txt").write_text("x")

        fetch_ok = MagicMock(return_value=_git_result())
        sync = MagicMock()
        with _patch_base_sync(
            target_fetch=fetch_ok,
            worktrees_root=MagicMock(return_value=wt_root),
            sync=sync,
        ):
            workflow._refresh_base_and_worktrees(self.gh, self.spec)

        called_numbers = sorted(recorded_call.args[3] for recorded_call in sync.call_args_list)
        self.assertEqual(called_numbers, [7, 42])

    def test_per_worktree_exception_is_swallowed(self) -> None:
        wt_root = self.tmpdir / "worktrees"
        wt_root.mkdir()
        (wt_root / "issue-1").mkdir()
        (wt_root / "issue-2").mkdir()
        fetch_ok = MagicMock(return_value=_git_result())
        sync = MagicMock(side_effect=[RuntimeError("kaboom"), None])
        with _patch_base_sync(
            target_fetch=fetch_ok,
            worktrees_root=MagicMock(return_value=wt_root),
            sync=sync,
        ):
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
            slug=PRIVATE_SLUG,
            target_root=self.target_root,
            base_branch=PRIVATE_BASE_BRANCH,
            remote_name=PRIVATE_REMOTE,
        )
        fetch = MagicMock(return_value=_git_result())
        plain_git = MagicMock(return_value=_git_result())

        with _patch_base_sync(
            target_fetch=fetch,
            git=plain_git,
            worktrees_root=MagicMock(return_value=self.tmpdir / "missing"),
        ):
            workflow._refresh_base_and_worktrees(self.gh, private_spec)

        self.assertEqual(
            fetch.call_args_list,
            [call(private_spec, PRIVATE_BASE_BRANCH)],
            "base refresh must route through `_authed_target_fetch` with the spec's base branch",
        )
        # No plain-git fetch was issued -- otherwise the multi-remote
        # token-selection regression resurfaces.
        for call_args in plain_git.call_args_list:
            args = call_args.args
            self.assertNotEqual(
                args[0] if args else "",
                "fetch",
                f'plain `_git("fetch", ...)` leaked: {args!r}',
            )
