# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""`fixing` label bootstrap, family-aware partitioning, PR-refresh
detour membership, dispatcher routing, the closed-issue sweep
inclusion, the no-`pr_number` park, the externally-merged /
closed-without-merge terminal arcs on a closed issue, the auto-merge
prohibition, and the `fixing` -> `resolving_conflict` rebase detour
that must preserve pending PR feedback bookmarks. The quiet-window /
dev-resume tests live in `tests/test_workflow_fixing.py`."""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

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
            number=801, head_branch="orchestrator/issue-705",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(705, pr_number=pr.number, branch="orchestrator/issue-705")

        mocks = self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((705, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(705))
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 705,
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
            number=802, head_branch="orchestrator/issue-706",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(706, pr_number=pr.number, branch="orchestrator/issue-706")

        mocks = self._run(
            lambda: workflow._process_issue(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((706, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(706))
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 706,
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
            number=901, head_branch="orchestrator/issue-720",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            approved=True,
        )
        gh.add_pr(pr)
        gh.seed_state(
            720, pr_number=pr.number,
            branch="orchestrator/issue-720",
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
    """A behind-base `fixing` worktree is detoured into
    `resolving_conflict` by the pre-tick refresh. The detour must NOT
    swallow pending PR feedback: the `pending_fix_*` bookmarks recorded
    by the in_review handoff and the in_review watermarks MUST survive
    the relabel, so the eventual return from `resolving_conflict` ->
    `validating` -> `in_review` re-discovers the unread feedback and
    routes it back to `fixing`.
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

    def test_fixing_detour_preserves_pending_feedback(self) -> None:
        # A `fixing` worktree that is N commits behind `origin/<base>`
        # must flip to `resolving_conflict` and PRESERVE the
        # `pending_fix_*` bookmarks and `pr_last_comment_id` watermark.
        # Any bump of those values here would silently consume the
        # unread feedback that triggered the original in_review ->
        # fixing route: when the resolving_conflict handler eventually
        # pushes the rebase and the validating -> in_review handoff
        # runs, the rescan would skip the (now-watermarked-past) human
        # comment and the in_review HITL ready-ping could advertise
        # the PR as ready for human merge over it.
        self.gh.add_issue(make_issue(7, label="fixing"))
        pr = FakePR(
            number=42, head_branch="orchestrator/issue-7",
            head=FakePRRef(sha="cafe1234"),
            state="open",
        )
        self.gh.add_pr(pr)
        self.gh.seed_state(
            7,
            pr_number=42,
            branch="orchestrator/issue-7",
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
        # Behind base by 3 commits drives the detour.
        git_mock = patch.object(
            base_sync, "_git",
            return_value=self._git_result(stdout="3\n"),
        )
        with patch.object(base_sync, "_worktree_dirty_files", return_value=[]), \
             git_mock:
            workflow._sync_worktree_with_base(self.gh, self.spec, self.wt, 7)

        # Detour fired: label flipped to resolving_conflict.
        self.assertIn((7, "resolving_conflict"), self.gh.label_history)
        # Pending-fix bookmarks survived the relabel so the eventual
        # in_review re-entry can correlate the triggering ids.
        data = self.gh.pinned_data(7)
        self.assertEqual(data.get("pending_fix_at"), "2026-05-23T00:00:00+00:00")
        self.assertEqual(data.get("pending_fix_issue_max_id"), 2000)
        self.assertEqual(data.get("pending_fix_review_max_id"), 3000)
        self.assertEqual(data.get("pending_fix_review_summary_max_id"), 4000)
        # And the in_review watermark is unchanged -- the rescan after
        # resolving_conflict -> validating -> in_review will surface
        # the original triggering comment as fresh feedback again.
        self.assertEqual(data.get("pr_last_comment_id"), 1999)
        self.assertEqual(data.get("pr_last_review_comment_id"), 0)
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)


if __name__ == "__main__":
    unittest.main()
