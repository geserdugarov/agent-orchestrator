# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing terminal routing behavior."""

from __future__ import annotations

import unittest

from tests import fixing_routing_test_support as support

AUTO_MERGE_ISSUE = support.AUTO_MERGE_ISSUE
AUTO_MERGE_PR = support.AUTO_MERGE_PR
BACKEND_CLAUDE = support.BACKEND_CLAUDE
CLOSED_POLLABLE_ISSUE = support.CLOSED_POLLABLE_ISSUE
CLOSED_WITHOUT_PR_ISSUE = support.CLOSED_WITHOUT_PR_ISSUE
DEV_SESSION = support.DEV_SESSION
FakeGitHubClient = support.FakeGitHubClient
FakePR = support.FakePR
FakePRRef = support.FakePRRef
IDEMPOTENT_PARK_ISSUE = support.IDEMPOTENT_PARK_ISSUE
INITIAL_COMMENT_WATERMARK = support.INITIAL_COMMENT_WATERMARK
ISSUE_FEEDBACK_ID = support.ISSUE_FEEDBACK_ID
KEY_AWAITING_HUMAN = support.KEY_AWAITING_HUMAN
LABEL_DONE = support.LABEL_DONE
LABEL_FIXING = support.LABEL_FIXING
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
LABEL_REJECTED = support.LABEL_REJECTED
MERGED_ISSUE = support.MERGED_ISSUE
MERGED_PR = support.MERGED_PR
MISSING_PR_ISSUE = support.MISSING_PR_ISSUE
OPEN_POLLABLE_ISSUE = support.OPEN_POLLABLE_ISSUE
PENDING_FIX_AT = support.PENDING_FIX_AT
PR_HEAD_SHA = support.PR_HEAD_SHA
STATE_CLOSED = support.STATE_CLOSED
UNMERGED_ISSUE = support.UNMERGED_ISSUE
UNMERGED_PR = support.UNMERGED_PR
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_TEST_SPEC = support._TEST_SPEC
_agent = support._agent
_issue_branch = support._issue_branch
make_issue = support.make_issue
workflow = support.workflow


def _assert_missing_pr_park_event(test_case, github) -> None:
    test_case.assertTrue(
        github.pinned_data(MISSING_PR_ISSUE).get(KEY_AWAITING_HUMAN),
    )
    events_for_issue = [
        event
        for event in github.recorded_events
        if event.get("issue") == MISSING_PR_ISSUE and event.get("event") == "park_awaiting_human"
    ]
    test_case.assertEqual(len(events_for_issue), 1)
    test_case.assertEqual(
        events_for_issue[0].get("reason"),
        "missing_pr_number",
    )
    test_case.assertEqual(github.label_history, [])


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
        _assert_missing_pr_park_event(self, gh)

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
            gh,
            _TEST_SPEC,
            MERGED_ISSUE,
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
            gh,
            _TEST_SPEC,
            UNMERGED_ISSUE,
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
            mergeable=True,
            check_state="success",
            approved=True,
        )
        gh.add_pr(pr)
        gh.seed_state(
            AUTO_MERGE_ISSUE,
            pr_number=pr.number,
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
