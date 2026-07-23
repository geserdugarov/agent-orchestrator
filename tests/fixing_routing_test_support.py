# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and protocol values for fixing routing tests."""

from __future__ import annotations

import contextlib
import pathlib
import shutil
import subprocess
import tempfile
from unittest import mock

from orchestrator import base_sync as _base_sync
from orchestrator import config, workflow
from tests import fakes, workflow_helpers

Path = pathlib.Path
MagicMock = mock.MagicMock
base_sync = _base_sync
patch = mock.patch
FakeGitHubClient = fakes.FakeGitHubClient
FakePR = fakes.FakePR
FakePRRef = fakes.FakePRRef
make_issue = fakes.make_issue
_PatchedWorkflowMixin = workflow_helpers._PatchedWorkflowMixin
_TEST_SPEC = workflow_helpers._TEST_SPEC
_agent = workflow_helpers._agent
_issue_branch = workflow_helpers._issue_branch
BACKEND_CLAUDE = "claude"
KEY_AWAITING_HUMAN = "awaiting_human"
LABEL_DONE = "done"
LABEL_FIXING = "fixing"
LABEL_IMPLEMENTING = "implementing"
LABEL_REJECTED = "rejected"
LABEL_RESOLVING_CONFLICT = "resolving_conflict"
LABEL_VALIDATING = "validating"
STATE_CLOSED = "closed"
STATE_OPEN = "open"
DEV_SESSION = "dev-sess"
PR_HEAD_SHA = "cafe1234"
PENDING_FIX_AT = "2026-05-23T00:00:00+00:00"
INITIAL_COMMENT_WATERMARK = 1999
ISSUE_FEEDBACK_ID = 2000
REVIEW_FEEDBACK_ID = 3000
SUMMARY_FEEDBACK_ID = 4000
DISPATCH_ISSUE = 701
MISSING_PR_ISSUE = 702
IDEMPOTENT_PARK_ISSUE = 703
CLOSED_WITHOUT_PR_ISSUE = 704
MERGED_ISSUE = 705
MERGED_PR = 801
UNMERGED_ISSUE = 706
UNMERGED_PR = 802
OPEN_POLLABLE_ISSUE = 710
CLOSED_POLLABLE_ISSUE = 711
AUTO_MERGE_ISSUE = 720
AUTO_MERGE_PR = 901
CONFLICT_FIXTURE_ISSUE = 7
CONFLICT_FIXTURE_PR = 42
DRIFT_PR_NUMBER_OFFSET = 900
DRIFT_FEEDBACK_WATERMARK = 5000
DRIFT_PR_HEAD = "prhead00cafe1234"
BEHIND_BASE_ISSUE = 30
UNPUSHED_REBASE_ISSUE = 34
IN_SYNC_ISSUE = 31
DIRTY_WORKTREE_ISSUE = 33
QUESTION_PARK_ISSUE = 35
REVIEW_TRANSIENT_ISSUE = 36
SILENT_PARK_ISSUE = 37


class _FixingConflictFixtureMixin:
    """A behind-base `fixing` worktree goes through the pre-tick base
    rebase. Both exits (clean rebase -> `validating`, conflicted rebase
    -> `resolving_conflict`) must PRESERVE the `pending_fix_*`
    bookmarks recorded by the in_review handoff and the in_review
    watermarks, so the eventual return from `validating` -> `in_review`
    re-discovers the unread feedback and routes it back to `fixing`.
    """

    def setUp(self) -> None:
        self.spec = config.RepoSpec(
            slug="acme/widget",
            target_root=Path("/tmp/refresh-target-fixing"),
            base_branch="main",
        )
        self.wt = Path("/tmp/refresh-wt-fixing")
        self.gh = FakeGitHubClient()

    def _git_result(self, *, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git"],
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )

    def _seed_fixing_with_pending_feedback(self) -> None:
        self.gh.add_issue(make_issue(CONFLICT_FIXTURE_ISSUE, label=LABEL_FIXING))
        pr = FakePR(
            number=CONFLICT_FIXTURE_PR,
            head_branch="orchestrator/acme__widget/issue-7",
            head=FakePRRef(sha=PR_HEAD_SHA),
            state=STATE_OPEN,
        )
        self.gh.add_pr(pr)
        self.gh.seed_state(
            CONFLICT_FIXTURE_ISSUE,
            pr_number=CONFLICT_FIXTURE_PR,
            branch="orchestrator/acme__widget/issue-7",
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            pr_last_comment_id=INITIAL_COMMENT_WATERMARK,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            pending_fix_at=PENDING_FIX_AT,
            pending_fix_issue_max_id=ISSUE_FEEDBACK_ID,
            pending_fix_review_max_id=REVIEW_FEEDBACK_ID,
            pending_fix_review_summary_max_id=SUMMARY_FEEDBACK_ID,
        )

    def _assert_pending_feedback_intact(self) -> None:
        # Pending-fix bookmarks survived the relabel so the eventual
        # in_review re-entry can correlate the triggering ids. The
        # in_review watermark is unchanged so the rescan after
        # `validating` -> `in_review` surfaces the original triggering
        # comment as fresh feedback again.
        pinned_data = self.gh.pinned_data(CONFLICT_FIXTURE_ISSUE)
        self.assertEqual(pinned_data.get("pending_fix_at"), PENDING_FIX_AT)
        self.assertEqual(pinned_data.get("pending_fix_issue_max_id"), ISSUE_FEEDBACK_ID)
        self.assertEqual(pinned_data.get("pending_fix_review_max_id"), REVIEW_FEEDBACK_ID)
        self.assertEqual(pinned_data.get("pending_fix_review_summary_max_id"), SUMMARY_FEEDBACK_ID)
        self.assertEqual(pinned_data.get("pr_last_comment_id"), INITIAL_COMMENT_WATERMARK)
        self.assertEqual(pinned_data.get("pr_last_review_comment_id"), 0)
        self.assertEqual(pinned_data.get("pr_last_review_summary_id"), 0)


class _FixingWorktreeDriftFixtureMixin:
    """A stuck validating-route transient can route through conflict handling.

    When a validating-route transient park (e.g. `push_failed`) cannot
    clear via the self-recovery (`_try_recover_validating_transient_park`
    returns "stuck"), `_handle_fixing` falls through to
    `_reconcile_parked_fixing` so a base advance that
    landed mid-park can still unstick the issue. The helper must hand
    both drift shapes to `resolving_conflict` while leaving any park
    that could be hiding a real dev question parked for the human.
    """

    def setUp(self) -> None:
        # The router probes `wt.exists()`, so the patched `_worktree_path`
        # must point at a directory that is really on disk.
        self._wt_dir = tempfile.mkdtemp(prefix="fixing-drift-wt-")
        self.addCleanup(shutil.rmtree, self._wt_dir, ignore_errors=True)

    def _git_behind(self, behind: int) -> MagicMock:
        return MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=f"{behind}\n",
                stderr="",
            )
        )

    def _seed_parked_fixing(
        self,
        gh: FakeGitHubClient,
        number: int,
        *,
        park_reason: str | None = "push_failed",
        pending_fix_at: str | None = None,
    ) -> None:
        issue = make_issue(number, label=LABEL_FIXING)
        gh.add_issue(issue)
        pr = FakePR(
            number=DRIFT_PR_NUMBER_OFFSET + number,
            head_branch=f"orchestrator/issue-{number}",
            head=FakePRRef(sha=DRIFT_PR_HEAD),
            state=STATE_OPEN,
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=pr.number,
            branch=f"orchestrator/issue-{number}",
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            awaiting_human=True,
            # Default: a stuck validating-route transient (`push_failed`)
            # with no `pending_fix_at` so the validating-route recovery
            # branch fires. Per-test overrides exercise the other shapes
            # the router must refuse to auto-recover.
            park_reason=park_reason,
            pending_fix_at=pending_fix_at,
            # Watermarks above any seeded comment so the rescan finds nothing.
            pr_last_comment_id=DRIFT_FEEDBACK_WATERMARK,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            review_round=1,
        )
        gh.seed_state(number, **state)

    @contextlib.contextmanager
    def _drift_patches(
        self,
        behind: int,
        *,
        dirty=(),
        local_head=DRIFT_PR_HEAD,
        recovery: str = "stuck",
    ):
        wt_path = Path(self._wt_dir)
        self.post = MagicMock()
        self.recover = MagicMock(return_value=recovery)
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    workflow,
                    "_worktree_path",
                    MagicMock(return_value=wt_path),
                )
            )
            stack.enter_context(
                patch.object(
                    workflow,
                    "_worktree_dirty_files",
                    MagicMock(return_value=list(dirty)),
                )
            )
            stack.enter_context(
                patch.object(
                    workflow,
                    "_git",
                    self._git_behind(behind),
                )
            )
            stack.enter_context(
                patch.object(
                    workflow,
                    "_head_sha",
                    MagicMock(return_value=local_head),
                )
            )
            stack.enter_context(
                patch.object(
                    workflow,
                    "_post_pr_comment",
                    self.post,
                )
            )
            stack.enter_context(
                patch.object(
                    workflow,
                    "_try_recover_validating_transient_park",
                    self.recover,
                )
            )
            yield

    def _assert_routed(self, gh, number) -> None:
        self.assertIn((number, LABEL_RESOLVING_CONFLICT), gh.label_history)
        pinned_data = gh.pinned_data(number)
        self.assertFalse(pinned_data.get(KEY_AWAITING_HUMAN))
        self.assertEqual(pinned_data.get("conflict_round"), 0)
        # The in_review watermark survives so the eventual in_review
        # re-entry can still re-discover any feedback past it.
        self.assertEqual(pinned_data.get("pr_last_comment_id"), DRIFT_FEEDBACK_WATERMARK)
        self.post.assert_called_once()
        entered = [
            event
            for event in gh.recorded_events
            if event.get("issue") == number
            and event.get("event") == "conflict_round"
            and event.get("action") == "entered"
        ]
        self.assertEqual(len(entered), 1)
        self.assertEqual(entered[0].get("stage"), LABEL_FIXING)
