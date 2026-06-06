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

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import base_sync, config, workflow

from tests.fakes import (
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class FixingLabelRoutingTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`fixing` is registered as a workflow label that sits between
    `in_review` and `validating` in the PR-feedback fix loop. The dispatcher
    must route the label to `_handle_fixing` instead of falling through to
    pickup or implementation, and the bootstrap specs / family-aware
    partitioning / closed-issue sweep / PR-worktree refresh detour must
    all recognise it as a PR-having stage. The PR-terminal arcs and the
    no-`pr_number` park covered here pair with the quiet-window / dev-
    resume tests in `tests/test_workflow_fixing.py`.
    """

    def test_fixing_label_is_recognized_as_workflow_label(self) -> None:
        from orchestrator.github import WORKFLOW_LABELS

        self.assertIn("fixing", WORKFLOW_LABELS)

    def test_fixing_label_is_in_bootstrap_specs(self) -> None:
        # Label bootstrap iterates WORKFLOW_LABEL_SPECS; if the spec entry
        # is missing, `ensure_workflow_labels` would never create the
        # label on a fresh repo and operators would be unable to apply it.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        self.assertIn("fixing", names)

    def test_fixing_label_sits_between_in_review_and_resolving_conflict(
        self,
    ) -> None:
        # Lifecycle order matters: `fixing` is the next stage after
        # `in_review` when the PR has fresh feedback. The spec tuple
        # encodes the lifecycle ordering, so it must place `fixing` right
        # after `in_review`.
        from orchestrator.github import WORKFLOW_LABEL_SPECS

        names = [name for name, _, _ in WORKFLOW_LABEL_SPECS]
        in_review_idx = names.index("in_review")
        fixing_idx = names.index("fixing")
        self.assertEqual(fixing_idx, in_review_idx + 1)

    def test_fixing_label_is_not_family_aware(self) -> None:
        # Open `fixing` issues touch only their own pinned state and PR
        # worktree, so the label must stay out of `_FAMILY_AWARE_LABELS` --
        # otherwise the parallel tick path would route it through the
        # single-threaded family bucket and defeat fan-out concurrency.
        self.assertNotIn("fixing", workflow._FAMILY_AWARE_LABELS)

    def test_fixing_label_is_in_pr_refresh_detour_set(self) -> None:
        # Behind-base PR-having worktrees need to be routed through
        # `resolving_conflict` by the pre-tick refresh; a `fixing` worktree
        # is PR-having (its sibling labels validating/in_review already
        # qualify) so it must be eligible for the same detour.
        from orchestrator.worktrees import _PR_REFRESH_DETOUR_LABELS

        self.assertIn("fixing", _PR_REFRESH_DETOUR_LABELS)

    def test_dispatcher_routes_fixing_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(701, label="fixing")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_fixing") as handler, \
             patch.object(workflow, "_handle_pickup") as pickup, \
             patch.object(workflow, "_handle_implementing") as impl, \
             patch.object(workflow, "_handle_in_review") as in_review:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)
        pickup.assert_not_called()
        impl.assert_not_called()
        in_review.assert_not_called()

    def test_fixing_without_pr_number_parks_awaiting_human(self) -> None:
        # A manual relabel directly to `fixing` without a recorded
        # `pr_number` cannot drive the dev-resume path (no PR to push
        # against). Park once, surfacing the misconfiguration to a
        # human; the label is left in place so the operator can fix
        # the relabel.
        gh = FakeGitHubClient()
        issue = make_issue(702, label="fixing")
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(len(gh.posted_comments), 1)
        issue_number, body = gh.posted_comments[0]
        self.assertEqual(issue_number, 702)
        self.assertIn("fixing", body)
        self.assertIn("pr_number", body)
        self.assertTrue(gh.pinned_data(702).get("awaiting_human"))
        # The `reason="missing_pr_number"` is recorded on the audit
        # event by `_park_awaiting_human`; the durable `park_reason`
        # field stays None (callers that need a transient/recoverable
        # tag re-set it explicitly -- this park is HITL-only).
        events_for_issue = [
            e for e in gh.recorded_events
            if e.get("issue") == 702
            and e.get("event") == "park_awaiting_human"
        ]
        self.assertEqual(len(events_for_issue), 1)
        self.assertEqual(events_for_issue[0].get("reason"), "missing_pr_number")
        # The label stays put: parking surfaces the situation but leaves
        # the operator in control of the next move.
        self.assertEqual(gh.label_history, [])

    def test_fixing_without_pr_number_is_idempotent_when_already_parked(
        self,
    ) -> None:
        # A second tick on an already-parked no-PR fixing issue must
        # not re-post the parking comment -- otherwise every polling
        # tick would spam the issue.
        gh = FakeGitHubClient()
        issue = make_issue(703, label="fixing")
        gh.add_issue(issue)
        gh.seed_state(703, awaiting_human=True)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)

    def test_fixing_skips_closed_issue_without_pr_number(self) -> None:
        # A closed-`fixing` issue with no recorded PR (manual relabel from
        # an early stage, no PR opened) cannot be finalized via the
        # PR-state arcs. The handler must NOT park (parking a closed issue
        # would spam a parking comment on a terminated thread); it leaves
        # the label alone and lets the operator relabel manually.
        gh = FakeGitHubClient()
        issue = make_issue(704, label="fixing")
        issue.closed = True
        gh.add_issue(issue)

        workflow._process_issue(gh, _TEST_SPEC, issue)

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.write_state_calls, 0)
        self.assertEqual(gh.label_history, [])

    def test_fixing_finalizes_closed_issue_on_external_merge(self) -> None:
        # The headline closed-sweep contract: a human merges the PR with
        # `Resolves #N` while the issue is labeled `fixing`. The issue
        # auto-closes; the closed-issue sweep yields it; the handler must
        # finalize to `done`, stamp `merged_at`, close (already closed),
        # and run branch cleanup -- otherwise the issue sits closed +
        # `fixing` forever.
        gh = FakeGitHubClient()
        issue = make_issue(705, label="fixing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=801, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-705",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(705, pr_number=pr.number, branch="orchestrator/geserdugarov__agent-orchestrator/issue-705")

        mocks = self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((705, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(705))
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 705,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-705",
        )

    def test_fixing_finalizes_closed_issue_on_closed_without_merge(
        self,
    ) -> None:
        # Mirror branch: PR was closed without merging while the issue
        # was in `fixing`. Handler must flip to `rejected`, stamp
        # `closed_without_merge_at`, and run branch cleanup.
        gh = FakeGitHubClient()
        issue = make_issue(706, label="fixing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=802, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-706",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(706, pr_number=pr.number, branch="orchestrator/geserdugarov__agent-orchestrator/issue-706")

        mocks = self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((706, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(706))
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 706,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-706",
        )

    def test_closed_fixing_issue_surfaces_in_pollable_sweep(self) -> None:
        # The closed-issue sweep has to include `fixing` so the handler
        # can finalize an externally-merged PR to `done` even when
        # `Resolves #N` already closed the issue.
        gh = FakeGitHubClient()
        open_impl = make_issue(710, label="implementing")
        closed_fixing = make_issue(711, label="fixing")
        closed_fixing.closed = True
        for i in (open_impl, closed_fixing):
            gh.add_issue(i)

        numbers = {i.number for i in gh.list_pollable_issues()}
        self.assertEqual(numbers, {710, 711})

    def test_auto_merge_does_not_fire_while_label_is_fixing(self) -> None:
        # Headline merge-safeguard contract: an approved + mergeable PR
        # whose linked issue is labeled `fixing` MUST NOT produce any
        # `gh.merge_pr` call. The orchestrator is permanently manual-
        # merge-only -- no handler calls `merge_pr` today -- but the
        # dispatcher also routes `fixing` to `_handle_fixing` (not
        # `_handle_in_review`), so a regression that smuggled a merge
        # call back into in_review would still not fire here. The
        # `merge_calls == []` assertion below catches either drift.
        gh = FakeGitHubClient()
        issue = make_issue(720, label="fixing")
        gh.add_issue(issue)
        pr = FakePR(
            number=901, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-720",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            approved=True,
        )
        gh.add_pr(pr)
        gh.seed_state(
            720, pr_number=pr.number,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-720",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=1999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Pending feedback recorded by the prior in_review tick.
            pending_fix_at="2026-05-23T00:00:00+00:00",
            pending_fix_issue_max_id=2000,
        )

        self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No merge call, no flip to done -- the dispatcher routed to
        # fixing, so the in_review merge path never ran.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((720, "done"), gh.label_history)


class FixingConflictDetourTest(unittest.TestCase):
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
        self.gh.add_issue(make_issue(7, label="fixing"))
        pr = FakePR(
            number=42, head_branch="orchestrator/acme__widget/issue-7",
            head=FakePRRef(sha="cafe1234"),
            state="open",
        )
        self.gh.add_pr(pr)
        self.gh.seed_state(
            7,
            pr_number=42,
            branch="orchestrator/acme__widget/issue-7",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=1999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            pending_fix_at="2026-05-23T00:00:00+00:00",
            pending_fix_issue_max_id=2000,
            pending_fix_review_max_id=3000,
            pending_fix_review_summary_max_id=4000,
        )

    def _assert_pending_feedback_intact(self) -> None:
        # Pending-fix bookmarks survived the relabel so the eventual
        # in_review re-entry can correlate the triggering ids. The
        # in_review watermark is unchanged so the rescan after
        # `validating` -> `in_review` surfaces the original triggering
        # comment as fresh feedback again.
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("pending_fix_at"), "2026-05-23T00:00:00+00:00")
        self.assertEqual(data.get("pending_fix_issue_max_id"), 2000)
        self.assertEqual(data.get("pending_fix_review_max_id"), 3000)
        self.assertEqual(data.get("pending_fix_review_summary_max_id"), 4000)
        self.assertEqual(data.get("pr_last_comment_id"), 1999)
        self.assertEqual(data.get("pr_last_review_comment_id"), 0)
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)

    def test_fixing_clean_rebase_preserves_pending_feedback(self) -> None:
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
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        # Clean rebase routed `fixing` straight to `validating`.
        self.assertIn((7, "validating"), self.gh.label_history)
        self.assertNotIn((7, "resolving_conflict"), self.gh.label_history)
        self._assert_pending_feedback_intact()

    def test_fixing_conflicting_rebase_preserves_pending_feedback(self) -> None:
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
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        self.assertIn((7, "resolving_conflict"), self.gh.label_history)
        self.assertNotIn((7, "validating"), self.gh.label_history)
        push.assert_not_called()
        self._assert_pending_feedback_intact()


class FixingWorktreeDriftRoutingTest(unittest.TestCase):
    """A stuck validating-route transient can route through conflict handling.

    When a validating-route transient park (e.g. `push_failed`) cannot
    clear via the self-recovery (`_try_recover_validating_transient_park`
    returns "stuck"), `_handle_fixing` falls through to
    `_route_parked_fixing_to_resolving_conflict` so a base advance that
    landed mid-park can still unstick the issue. The router must hand
    both drift shapes to `resolving_conflict` while leaving any park that
    could be hiding a real dev question parked for the human.
    """

    PR_HEAD = "prhead00cafe1234"

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
        extra_labels=(),
        park_reason: str | None = "push_failed",
        pending_fix_at: str | None = None,
    ) -> None:
        issue = make_issue(number, label="fixing")
        for name in extra_labels:
            issue.labels.append(FakeLabel(name))
        gh.add_issue(issue)
        pr = FakePR(
            number=900 + number,
            head_branch=f"orchestrator/issue-{number}",
            head=FakePRRef(sha=self.PR_HEAD),
            state="open",
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=pr.number,
            branch=f"orchestrator/issue-{number}",
            dev_agent="claude",
            dev_session_id="dev-sess",
            awaiting_human=True,
            # Default: a stuck validating-route transient (`push_failed`)
            # with no `pending_fix_at` so the validating-route recovery
            # branch fires. Per-test overrides exercise the other shapes
            # the router must refuse to auto-recover.
            park_reason=park_reason,
            pending_fix_at=pending_fix_at,
            # Watermarks above any seeded comment so the rescan finds nothing.
            pr_last_comment_id=5000,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            review_round=1,
        )
        gh.seed_state(number, **state)

    def _drift_patches(
        self,
        behind: int,
        *,
        dirty=(),
        local_head=PR_HEAD,
        recovery: str = "stuck",
    ):
        wt_path = Path(self._wt_dir)
        self.post = MagicMock()
        self.recover = MagicMock(return_value=recovery)
        return (
            patch.object(
                workflow, "_worktree_path", MagicMock(return_value=wt_path),
            ),
            patch.object(
                workflow, "_worktree_dirty_files",
                MagicMock(return_value=list(dirty)),
            ),
            patch.object(workflow, "_git", self._git_behind(behind)),
            patch.object(
                workflow, "_head_sha", MagicMock(return_value=local_head),
            ),
            patch.object(workflow, "_post_pr_comment", self.post),
            patch.object(
                workflow, "_try_recover_validating_transient_park",
                self.recover,
            ),
        )

    def _assert_routed(self, gh, number) -> None:
        self.assertIn((number, "resolving_conflict"), gh.label_history)
        data = gh.pinned_data(number)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("conflict_round"), 0)
        # The in_review watermark survives so the eventual in_review
        # re-entry can still re-discover any feedback past it.
        self.assertEqual(data.get("pr_last_comment_id"), 5000)
        self.post.assert_called_once()
        entered = [
            e for e in gh.recorded_events
            if e.get("issue") == number
            and e.get("event") == "conflict_round"
            and e.get("action") == "entered"
        ]
        self.assertEqual(len(entered), 1)
        self.assertEqual(entered[0].get("stage"), "fixing")

    def test_stuck_push_failed_behind_base_routes(self) -> None:
        # Variant 1: stuck `push_failed` + worktree behind base ->
        # resolving_conflict rebases.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, 30)
        p1, p2, p3, p4, p5, p6 = self._drift_patches(2)
        with p1, p2, p3, p4, p5, p6:
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(30))
        self._assert_routed(gh, 30)
        self.recover.assert_called_once()

    def test_stuck_push_failed_unpushed_rebase_routes(self) -> None:
        # Variant 2: stuck `push_failed` + worktree ON base but local HEAD
        # differs from the stale remote PR head -> resolving_conflict
        # recognises the already-rebased worktree and republishes it.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, 34)
        p1, p2, p3, p4, p5, p6 = self._drift_patches(0, local_head="079210cabc")
        with p1, p2, p3, p4, p5, p6:
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(34))
        self._assert_routed(gh, 34)

    def test_stuck_push_failed_in_sync_stays_parked(self) -> None:
        # On base AND local HEAD == PR head: drift is not the underlying
        # blocker. The recovery already declared "stuck" -> bail silently
        # so the human can investigate, do not re-post any comment.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, 31)
        p1, p2, p3, p4, p5, p6 = self._drift_patches(0, local_head=self.PR_HEAD)
        with p1, p2, p3, p4, p5, p6:
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(31))

        self.assertNotIn((31, "resolving_conflict"), gh.label_history)
        self.assertTrue(gh.pinned_data(31).get("awaiting_human"))
        self.post.assert_not_called()

    def test_stuck_push_failed_held_stays_parked(self) -> None:
        # `hold_base_sync` is an explicit operator pause; respect it even
        # when the worktree is behind base.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, 32, extra_labels=("hold_base_sync",))
        p1, p2, p3, p4, p5, p6 = self._drift_patches(5)
        with p1, p2, p3, p4, p5, p6:
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(32))

        self.assertNotIn((32, "resolving_conflict"), gh.label_history)
        self.assertTrue(gh.pinned_data(32).get("awaiting_human"))
        self.post.assert_not_called()

    def test_stuck_push_failed_dirty_stays_parked(self) -> None:
        # A dirty worktree is a park an operator may be inspecting;
        # `resolving_conflict` would reset it to the remote, so leave it.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, 33)
        p1, p2, p3, p4, p5, p6 = self._drift_patches(5, dirty=("src/x.py",))
        with p1, p2, p3, p4, p5, p6:
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(33))

        self.assertNotIn((33, "resolving_conflict"), gh.label_history)
        self.assertTrue(gh.pinned_data(33).get("awaiting_human"))
        self.post.assert_not_called()

    def test_question_park_with_drift_stays_parked(self) -> None:
        # A `park_reason=None` `_on_question` shape could be a real agent
        # question or a "nothing to fix" remark; route neither by inspection.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, 35, park_reason=None)
        p1, p2, p3, p4, p5, p6 = self._drift_patches(7)
        with p1, p2, p3, p4, p5, p6:
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(35))

        self.assertNotIn((35, "resolving_conflict"), gh.label_history)
        self.assertTrue(gh.pinned_data(35).get("awaiting_human"))
        self.post.assert_not_called()
        self.recover.assert_not_called()

    def test_in_review_route_transient_with_drift_stays_parked(self) -> None:
        # In_review-route transient parks (`pending_fix_at` set) are
        # deliberately NOT auto-recovered: the round and watermark
        # semantics differ from the validating route.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(
            gh, 36, pending_fix_at="2026-05-23T00:00:00+00:00",
        )
        p1, p2, p3, p4, p5, p6 = self._drift_patches(4)
        with p1, p2, p3, p4, p5, p6:
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(36))

        self.assertNotIn((36, "resolving_conflict"), gh.label_history)
        self.assertTrue(gh.pinned_data(36).get("awaiting_human"))
        self.post.assert_not_called()
        self.recover.assert_not_called()

    def test_stuck_silent_park_with_drift_stays_parked(self) -> None:
        # `agent_silent` is not in `_VALIDATING_TRANSIENT_PARK_REASONS`
        # (the silent-crash counter is the recovery channel, not drift)
        # so even with `pending_fix_at` unset the issue must stay parked.
        gh = FakeGitHubClient()
        self._seed_parked_fixing(gh, 37, park_reason="agent_silent")
        p1, p2, p3, p4, p5, p6 = self._drift_patches(3)
        with p1, p2, p3, p4, p5, p6:
            workflow._handle_fixing(gh, _TEST_SPEC, gh.get_issue(37))

        self.assertNotIn((37, "resolving_conflict"), gh.label_history)
        self.assertTrue(gh.pinned_data(37).get("awaiting_human"))
        self.post.assert_not_called()
        self.recover.assert_not_called()

if __name__ == "__main__":
    unittest.main()
