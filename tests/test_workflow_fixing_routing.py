# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""`fixing` label bootstrap, family-aware partitioning, PR-refresh
membership, dispatcher routing, the closed-issue sweep inclusion,
the no-`pr_number` park, the externally-merged / closed-without-merge
terminal arcs on a closed issue, the auto-merge prohibition, and the
pre-tick base rebase that must preserve pending PR feedback bookmarks
across BOTH refresh exits (clean rebase -> `validating`; rebase
leaves conflicted files -> `resolving_conflict`). The quiet-window /
dev-resume tests live in `tests/test_workflow_fixing.py`."""
from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import base_sync, config, workflow

from tests.fakes import (
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _issue_branch,
)

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


class FixingLabelDefinitionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`fixing` is registered as a workflow label that sits between
    `in_review` and `validating` in the PR-feedback fix loop. The dispatcher
    must route the label to `_handle_fixing` instead of falling through to
    pickup or implementation, and the bootstrap specs / family-aware
    partitioning / closed-issue sweep / PR-worktree refresh detour must
    all recognise it as a PR-having stage. The PR-terminal arcs and the
    no-`pr_number` park covered here pair with the quiet-window / dev-
    resume tests in `tests/test_workflow_fixing.py`.
    """

    def test_label_is_recognized(self) -> None:
        from orchestrator.github import WORKFLOW_LABELS

        self.assertIn(LABEL_FIXING, WORKFLOW_LABELS)

    def test_fixing_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        self.assertIn(LABEL_FIXING, names)

    def test_label_between_review_and_conflict(
        self,
    ) -> None:
        # Lifecycle order matters: `fixing` is the next stage after
        # `in_review` when the PR has fresh feedback. The spec tuple
        # encodes the lifecycle ordering, so it must place `fixing` right
        # after `in_review`.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        in_review_idx = names.index("in_review")
        fixing_idx = names.index(LABEL_FIXING)
        self.assertEqual(fixing_idx, in_review_idx + 1)

    def test_fixing_label_is_not_family_aware(self) -> None:
        # Open `fixing` issues touch only their own pinned state and PR
        # worktree, so the label must stay out of `_FAMILY_AWARE_LABELS` --
        # otherwise the parallel tick path would route it through the
        # single-threaded family bucket and defeat fan-out concurrency.
        self.assertNotIn(LABEL_FIXING, workflow._FAMILY_AWARE_LABELS)

    def test_fixing_label_is_in_pr_refresh_detour_set(self) -> None:
        # Behind-base PR-having worktrees need to be routed through
        # `resolving_conflict` by the pre-tick refresh; a `fixing` worktree
        # is PR-having (its sibling labels validating/in_review already
        # qualify) so it must be eligible for the same detour.
        from orchestrator.worktrees import _PR_REFRESH_DETOUR_LABELS

        self.assertIn(LABEL_FIXING, _PR_REFRESH_DETOUR_LABELS)

    def test_dispatcher_routes_fixing_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(DISPATCH_ISSUE, label=LABEL_FIXING)
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_fixing") as fixing_handler, \
             patch.object(workflow, "_handle_pickup") as pickup, \
             patch.object(workflow, "_handle_implementing") as impl, \
             patch.object(workflow, "_handle_in_review") as in_review:
            workflow._process_issue(gh, _TEST_SPEC, issue)
            fixing_handler.assert_called_once_with(gh, _TEST_SPEC, issue)
            pickup.assert_not_called()
            impl.assert_not_called()
            in_review.assert_not_called()


class FixingTerminalRoutingTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_missing_pr_parks_awaiting_human(self) -> None:
        # A manual relabel directly to `fixing` without a recorded
        # `pr_number` cannot drive the dev-resume path (no PR to push
        # against). Park once, surfacing the misconfiguration to a
        # human; the label is left in place so the operator can fix
        # the relabel.
        gh = FakeGitHubClient()
        issue = make_issue(MISSING_PR_ISSUE, label=LABEL_FIXING)
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(len(gh.posted_comments), 1)
        issue_number, body = gh.posted_comments[0]
        self.assertEqual(issue_number, MISSING_PR_ISSUE)
        self.assertIn(LABEL_FIXING, body)
        self.assertIn("pr_number", body)
        self.assertTrue(gh.pinned_data(MISSING_PR_ISSUE).get(KEY_AWAITING_HUMAN))
        # The `reason="missing_pr_number"` is recorded on the audit
        # event by `_park_awaiting_human`; the durable `park_reason`
        # field stays None (callers that need a transient/recoverable
        # tag re-set it explicitly -- this park is HITL-only).
        events_for_issue = [
            event for event in gh.recorded_events
            if event.get("issue") == MISSING_PR_ISSUE
            and event.get("event") == "park_awaiting_human"
        ]
        self.assertEqual(len(events_for_issue), 1)
        self.assertEqual(events_for_issue[0].get("reason"), "missing_pr_number")
        # The label stays put: parking surfaces the situation but leaves
        # the operator in control of the next move.
        self.assertEqual(gh.label_history, [])

    def test_missing_pr_park_is_idempotent(
        self,
    ) -> None:
        # A second tick on an already-parked no-PR fixing issue must
        # not re-post the parking comment -- otherwise every polling
        # tick would spam the issue.
        gh = FakeGitHubClient()
        issue = make_issue(IDEMPOTENT_PARK_ISSUE, label=LABEL_FIXING)
        gh.add_issue(issue)
        gh.seed_state(IDEMPOTENT_PARK_ISSUE, awaiting_human=True)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)

    def test_closed_issue_without_pr_is_skipped(self) -> None:
        # A closed-`fixing` issue with no recorded PR (manual relabel from
        # an early stage, no PR opened) cannot be finalized via the
        # PR-state arcs. The handler must NOT park (parking a closed issue
        # would spam a parking comment on a terminated thread); it leaves
        # the label alone and lets the operator relabel manually.
        gh = FakeGitHubClient()
        issue = make_issue(CLOSED_WITHOUT_PR_ISSUE, label=LABEL_FIXING)
        issue.closed = True
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)
        self.assertEqual(gh.label_history, [])

    def test_external_merge_finalizes_closed_issue(self) -> None:
        # The headline closed-sweep contract: a human merges the PR with
        # `Resolves #N` while the issue is labeled `fixing`. The issue
        # auto-closes; the closed-issue sweep yields it; the handler must
        # finalize to `done`, stamp `merged_at`, close (already closed),
        # and run branch cleanup -- otherwise the issue sits closed +
        # `fixing` forever.
        gh = FakeGitHubClient()
        issue = make_issue(MERGED_ISSUE, label=LABEL_FIXING)
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=MERGED_PR,
            head_branch=_issue_branch(MERGED_ISSUE),
            head=FakePRRef(sha=PR_HEAD_SHA),
            merged=True,
            state=STATE_CLOSED,
        )
        gh.add_pr(pr)
        gh.seed_state(MERGED_ISSUE, pr_number=pr.number, branch=_issue_branch(MERGED_ISSUE))

        mocks = self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((MERGED_ISSUE, LABEL_DONE), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(MERGED_ISSUE))
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, MERGED_ISSUE,
            branch=_issue_branch(MERGED_ISSUE),
        )

    def test_closed_unmerged_pr_finalizes_issue(
        self,
    ) -> None:
        # Mirror branch: PR was closed without merging while the issue
        # was in `fixing`. Handler must flip to `rejected`, stamp
        # `closed_without_merge_at`, and run branch cleanup.
        gh = FakeGitHubClient()
        issue = make_issue(UNMERGED_ISSUE, label=LABEL_FIXING)
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=UNMERGED_PR,
            head_branch=_issue_branch(UNMERGED_ISSUE),
            head=FakePRRef(sha=PR_HEAD_SHA),
            merged=False,
            state=STATE_CLOSED,
        )
        gh.add_pr(pr)
        gh.seed_state(UNMERGED_ISSUE, pr_number=pr.number, branch=_issue_branch(UNMERGED_ISSUE))

        mocks = self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((UNMERGED_ISSUE, LABEL_REJECTED), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(UNMERGED_ISSUE))
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, UNMERGED_ISSUE,
            branch=_issue_branch(UNMERGED_ISSUE),
        )

    def test_closed_issue_is_in_pollable_sweep(self) -> None:
        # The closed-issue sweep has to include `fixing` so the handler
        # can finalize an externally-merged PR to `done` even when
        # `Resolves #N` already closed the issue.
        gh = FakeGitHubClient()
        open_impl = make_issue(OPEN_POLLABLE_ISSUE, label=LABEL_IMPLEMENTING)
        closed_fixing = make_issue(CLOSED_POLLABLE_ISSUE, label=LABEL_FIXING)
        closed_fixing.closed = True
        for pollable_issue in (open_impl, closed_fixing):
            gh.add_issue(pollable_issue)

        numbers = {issue.number for issue in gh.list_pollable_issues()}
        self.assertEqual(numbers, {OPEN_POLLABLE_ISSUE, CLOSED_POLLABLE_ISSUE})

    def test_auto_merge_skips_fixing_label(self) -> None:
        # Headline merge-safeguard contract: an approved + mergeable PR
        # whose linked issue is labeled `fixing` MUST NOT produce any
        # `gh.merge_pr` call. The orchestrator is permanently manual-
        # merge-only -- no handler calls `merge_pr` today -- but the
        # dispatcher also routes `fixing` to `_handle_fixing` (not
        # `_handle_in_review`), so a regression that smuggled a merge
        # call back into in_review would still not fire here. The
        # `merge_calls == []` assertion below catches either drift.
        gh = FakeGitHubClient()
        issue = make_issue(AUTO_MERGE_ISSUE, label=LABEL_FIXING)
        gh.add_issue(issue)
        pr = FakePR(
            number=AUTO_MERGE_PR,
            head_branch=_issue_branch(AUTO_MERGE_ISSUE),
            head=FakePRRef(sha=PR_HEAD_SHA),
            mergeable=True, check_state="success",
            approved=True,
        )
        gh.add_pr(pr)
        gh.seed_state(
            AUTO_MERGE_ISSUE, pr_number=pr.number,
            branch=_issue_branch(AUTO_MERGE_ISSUE),
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            pr_last_comment_id=INITIAL_COMMENT_WATERMARK,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Pending feedback recorded by the prior in_review tick.
            pending_fix_at=PENDING_FIX_AT,
            pending_fix_issue_max_id=ISSUE_FEEDBACK_ID,
        )

        self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No merge call, no flip to done -- the dispatcher routed to
        # fixing, so the in_review merge path never ran.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((AUTO_MERGE_ISSUE, LABEL_DONE), gh.label_history)


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

    def _git_result(
        self, *, returncode: int = 0, stdout: str = ""
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git"], returncode=returncode, stdout=stdout, stderr="",
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


class FixingConflictDetourTest(
    _FixingConflictFixtureMixin, unittest.TestCase,
):
    def test_clean_rebase_keeps_pending_feedback(self) -> None:
        # A clean refresh-time rebase now routes the `fixing` issue to
        # `validating` (no longer to `resolving_conflict`). Either way
        # the pending-fix bookmarks and in_review watermarks must
        # survive the relabel.
        from unittest.mock import MagicMock

        self._seed_fixing_with_pending_feedback()
        merge = MagicMock(return_value=(True, []))
        push = MagicMock(return_value=True)
        head_sha = MagicMock(side_effect=["before", "after"])
        git_mock = patch.object(
            base_sync, "_git",
            return_value=self._git_result(stdout="3\n"),
        )
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             git_mock:
            workflow._sync_worktree_with_base(
                self.gh,
                self.spec,
                self.wt,
                CONFLICT_FIXTURE_ISSUE,
            )

        # Clean rebase routed `fixing` straight to `validating`.
        self.assertIn((CONFLICT_FIXTURE_ISSUE, LABEL_VALIDATING), self.gh.label_history)
        self.assertNotIn((CONFLICT_FIXTURE_ISSUE, LABEL_RESOLVING_CONFLICT), self.gh.label_history)
        self._assert_pending_feedback_intact()

    def test_conflict_rebase_keeps_pending_feedback(self) -> None:
        # A conflicting refresh-time rebase still routes to
        # `resolving_conflict` so the handler can drive the dev agent.
        # The pending-fix bookmarks and watermarks must survive that
        # relabel too.
        from unittest.mock import MagicMock

        self._seed_fixing_with_pending_feedback()
        merge = MagicMock(return_value=(False, ["src/feature.py"]))
        push = MagicMock()
        head_sha = MagicMock(return_value="before")
        hardened = MagicMock(return_value=self._git_result())
        git_mock = patch.object(
            base_sync, "_git",
            return_value=self._git_result(stdout="3\n"),
        )
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             patch.object(base_sync, "_rebase_base_into_worktree", merge), \
             patch.object(base_sync, "_push_branch", push), \
             patch.object(base_sync, "_head_sha", head_sha), \
             patch.object(base_sync, "_git_hardened", hardened), \
             git_mock:
            workflow._sync_worktree_with_base(
                self.gh,
                self.spec,
                self.wt,
                CONFLICT_FIXTURE_ISSUE,
            )

        self.assertIn((CONFLICT_FIXTURE_ISSUE, LABEL_RESOLVING_CONFLICT), self.gh.label_history)
        self.assertNotIn((CONFLICT_FIXTURE_ISSUE, LABEL_VALIDATING), self.gh.label_history)
        push.assert_not_called()
        self._assert_pending_feedback_intact()


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
                args=["git"], returncode=0, stdout=f"{behind}\n", stderr="",
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
            stack.enter_context(patch.object(
                workflow, "_worktree_path", MagicMock(return_value=wt_path),
            ))
            stack.enter_context(patch.object(
                workflow, "_worktree_dirty_files",
                MagicMock(return_value=list(dirty)),
            ))
            stack.enter_context(patch.object(
                workflow, "_git", self._git_behind(behind),
            ))
            stack.enter_context(patch.object(
                workflow, "_head_sha", MagicMock(return_value=local_head),
            ))
            stack.enter_context(patch.object(
                workflow, "_post_pr_comment", self.post,
            ))
            stack.enter_context(patch.object(
                workflow, "_try_recover_validating_transient_park",
                self.recover,
            ))
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
            event for event in gh.recorded_events
            if event.get("issue") == number
            and event.get("event") == "conflict_round"
            and event.get("action") == "entered"
        ]
        self.assertEqual(len(entered), 1)
        self.assertEqual(entered[0].get("stage"), LABEL_FIXING)


class FixingWorktreeDriftRoutingTest(
    _FixingWorktreeDriftFixtureMixin, unittest.TestCase,
):
    def test_stuck_push_failed_behind_base_routes(self) -> None:
        # Variant 1: stuck `push_failed` + worktree behind base ->
        # resolving_conflict rebases.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, BEHIND_BASE_ISSUE)
        with self._drift_patches(2):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(BEHIND_BASE_ISSUE))
        self._assert_routed(gh, BEHIND_BASE_ISSUE)
        self.recover.assert_called_once()

    def test_stuck_push_failed_unpushed_rebase_routes(self) -> None:
        # Variant 2: stuck `push_failed` + worktree ON base but local HEAD
        # differs from the stale remote PR head -> resolving_conflict
        # recognises the already-rebased worktree and republishes it.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, UNPUSHED_REBASE_ISSUE)
        with self._drift_patches(0, local_head="079210cabc"):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(UNPUSHED_REBASE_ISSUE))
        self._assert_routed(gh, UNPUSHED_REBASE_ISSUE)

    def test_stuck_push_failed_in_sync_stays_parked(self) -> None:
        # On base AND local HEAD == PR head: drift is not the underlying
        # blocker. The recovery already declared "stuck" -> bail silently
        # so the human can investigate, do not re-post any comment.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, IN_SYNC_ISSUE)
        with self._drift_patches(0, local_head=DRIFT_PR_HEAD):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(IN_SYNC_ISSUE))

        self.assertNotIn((IN_SYNC_ISSUE, LABEL_RESOLVING_CONFLICT), gh.label_history)
        self.assertTrue(gh.pinned_data(IN_SYNC_ISSUE).get(KEY_AWAITING_HUMAN))
        self.post.assert_not_called()

    def test_stuck_push_failed_dirty_stays_parked(self) -> None:
        # A dirty worktree is a park an operator may be inspecting;
        # `resolving_conflict` would reset it to the remote, so leave it.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, DIRTY_WORKTREE_ISSUE)
        with self._drift_patches(5, dirty=("src/x.py",)):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(DIRTY_WORKTREE_ISSUE))

        self.assertNotIn((DIRTY_WORKTREE_ISSUE, LABEL_RESOLVING_CONFLICT), gh.label_history)
        self.assertTrue(gh.pinned_data(DIRTY_WORKTREE_ISSUE).get(KEY_AWAITING_HUMAN))
        self.post.assert_not_called()

    def test_question_park_with_drift_stays_parked(self) -> None:
        # A `park_reason=None` `_on_question` shape could be a real agent
        # question or a "nothing to fix" remark; route neither by inspection.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, QUESTION_PARK_ISSUE, park_reason=None)
        with self._drift_patches(7):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(QUESTION_PARK_ISSUE))

        self.assertNotIn((QUESTION_PARK_ISSUE, LABEL_RESOLVING_CONFLICT), gh.label_history)
        self.assertTrue(gh.pinned_data(QUESTION_PARK_ISSUE).get(KEY_AWAITING_HUMAN))
        self.post.assert_not_called()
        self.recover.assert_not_called()

    def test_review_transient_drift_stays_parked(self) -> None:
        # In_review-route transient parks (`pending_fix_at` set) are
        # deliberately NOT auto-recovered: the round and watermark
        # semantics differ from the validating route.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(
            gh, REVIEW_TRANSIENT_ISSUE, pending_fix_at=PENDING_FIX_AT,
        )
        with self._drift_patches(4):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(REVIEW_TRANSIENT_ISSUE))

        self.assertNotIn((REVIEW_TRANSIENT_ISSUE, LABEL_RESOLVING_CONFLICT), gh.label_history)
        self.assertTrue(gh.pinned_data(REVIEW_TRANSIENT_ISSUE).get(KEY_AWAITING_HUMAN))
        self.post.assert_not_called()
        self.recover.assert_not_called()

    def test_silent_park_with_drift_stays_parked(self) -> None:
        # `agent_silent` is not in `_VALIDATING_TRANSIENT_PARK_REASONS`
        # (the silent-crash counter is the recovery channel, not drift)
        # so even with `pending_fix_at` unset the issue must stay parked.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, SILENT_PARK_ISSUE, park_reason="agent_silent")
        with self._drift_patches(3):
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(SILENT_PARK_ISSUE))

        self.assertNotIn((SILENT_PARK_ISSUE, LABEL_RESOLVING_CONFLICT), gh.label_history)
        self.assertTrue(gh.pinned_data(SILENT_PARK_ISSUE).get(KEY_AWAITING_HUMAN))
        self.post.assert_not_called()
        self.recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
