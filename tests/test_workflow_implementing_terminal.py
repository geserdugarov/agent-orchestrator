# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Terminal handling for `_handle_implementing`: external-merge short-circuit
to `done` and the closed-issue sweep that flips to `rejected` (with safe
deferrals for transient PR-fetch failures), plus direct coverage of the
terminal usage receipt the shared `_finalize_if_issue_closed` helper posts
before its pinned-state write."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from orchestrator import workflow

from tests.fakes import (
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_helpers import (
    EVENT_PR_CLOSED_WITHOUT_MERGE,
    LABEL_IMPLEMENTING,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _issue_branch,
)

RUN_AGENT = "run_agent"
CLEANUP_TERMINAL_BRANCH = "_cleanup_terminal_branch"
LABEL_DONE = "done"
LABEL_REJECTED = "rejected"
CLOSED_WITHOUT_MERGE_AT = "closed_without_merge_at"
PR_HEAD_SHA = "cafe1234"
DEV_AGENT = "claude"
DEV_SESSION = "dev-sess"

EXTERNALLY_MERGED_ISSUE = 150
EXTERNALLY_MERGED_PR = 15000
NO_PR_ISSUE = 151
OPEN_PR_ISSUE = 152
OPEN_PR = 15200
CLOSED_PR_ISSUE = 153
CLOSED_PR = 15300
FETCH_FAILURE_ISSUE = 154
FETCH_FAILURE_PR = 15400
MERGED_DEFER_ISSUE = 155
MERGED_DEFER_PR = 15500
USAGE_ISSUE = 156
NO_USAGE_ISSUE = 157
USAGE_TOKEN_COUNT = 3400
USAGE_COST_USD = 0.31


class HandleImplementingExternalMergeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human merged the PR before implementing finished (e.g. an
    operator cherry-picked the work elsewhere). The handler must
    short-circuit to `done` instead of resuming the dev agent.
    """

    def test_external_merge_finalizes_to_done(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(EXTERNALLY_MERGED_ISSUE, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        pr = FakePR(
            number=EXTERNALLY_MERGED_PR,
            head_branch=_issue_branch(EXTERNALLY_MERGED_ISSUE),
            head=FakePRRef(sha=PR_HEAD_SHA),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            EXTERNALLY_MERGED_ISSUE,
            pr_number=EXTERNALLY_MERGED_PR,
            branch=_issue_branch(EXTERNALLY_MERGED_ISSUE),
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
        )

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(),
        )

        self.assertIn((EXTERNALLY_MERGED_ISSUE, LABEL_DONE), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(EXTERNALLY_MERGED_ISSUE))
        self.assertTrue(issue.closed)
        mocks[RUN_AGENT].assert_not_called()
        mocks[CLEANUP_TERMINAL_BRANCH].assert_called_once_with(
            gh, _TEST_SPEC, EXTERNALLY_MERGED_ISSUE,
            branch=_issue_branch(EXTERNALLY_MERGED_ISSUE),
        )


class HandleImplementingClosedIssueTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Closed `implementing` issues yielded by the new closed-issue sweep
    must NOT spawn the dev agent. The handler now flips to `rejected`
    after the external-merge finalize returns False.
    """

    def test_no_pr_flips_to_rejected(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(NO_PR_ISSUE, label=LABEL_IMPLEMENTING)
        issue.closed = True
        gh.add_issue(issue)
        gh.seed_state(NO_PR_ISSUE, dev_agent=DEV_AGENT, dev_session_id=DEV_SESSION)

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(),
        )

        self.assertIn((NO_PR_ISSUE, LABEL_REJECTED), gh.label_history)
        self.assertIn(CLOSED_WITHOUT_MERGE_AT, gh.pinned_data(NO_PR_ISSUE))
        mocks[RUN_AGENT].assert_not_called()
        # No PR → no branch cleanup (no remote ref to delete).
        mocks[CLEANUP_TERMINAL_BRANCH].assert_not_called()

    def test_open_pr_skips_cleanup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(OPEN_PR_ISSUE, label=LABEL_IMPLEMENTING)
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=OPEN_PR,
            head_branch=_issue_branch(OPEN_PR_ISSUE),
            head=FakePRRef(sha=PR_HEAD_SHA),
            merged=False,
            state="open",
        )
        gh.add_pr(pr)
        gh.seed_state(
            OPEN_PR_ISSUE,
            pr_number=OPEN_PR,
            branch=_issue_branch(OPEN_PR_ISSUE),
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
        )

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(),
        )

        self.assertIn((OPEN_PR_ISSUE, LABEL_REJECTED), gh.label_history)
        self.assertIn(CLOSED_WITHOUT_MERGE_AT, gh.pinned_data(OPEN_PR_ISSUE))
        mocks[RUN_AGENT].assert_not_called()
        # Open PR + closed issue: leave the branch alone so the operator
        # can salvage / reopen the PR.
        mocks[CLEANUP_TERMINAL_BRANCH].assert_not_called()

    def test_closed_pr_runs_cleanup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(CLOSED_PR_ISSUE, label=LABEL_IMPLEMENTING)
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=CLOSED_PR,
            head_branch=_issue_branch(CLOSED_PR_ISSUE),
            head=FakePRRef(sha=PR_HEAD_SHA),
            merged=False,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            CLOSED_PR_ISSUE,
            pr_number=CLOSED_PR,
            branch=_issue_branch(CLOSED_PR_ISSUE),
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
        )

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(),
        )

        self.assertIn((CLOSED_PR_ISSUE, LABEL_REJECTED), gh.label_history)
        self.assertIn(CLOSED_WITHOUT_MERGE_AT, gh.pinned_data(CLOSED_PR_ISSUE))
        mocks[RUN_AGENT].assert_not_called()
        mocks[CLEANUP_TERMINAL_BRANCH].assert_called_once_with(
            gh, _TEST_SPEC, CLOSED_PR_ISSUE,
            branch=_issue_branch(CLOSED_PR_ISSUE),
        )
        # `pr_closed_without_merge` event emitted only when the PR
        # itself is closed (mirrors in_review / fixing semantics).
        kinds = [event["event"] for event in gh.recorded_events]
        self.assertIn(EVENT_PR_CLOSED_WITHOUT_MERGE, kinds)

    def test_pr_fetch_error_defers(self) -> None:
        # Both `_finalize_if_pr_merged` and `_finalize_if_issue_closed`
        # need a successful `gh.get_pr` call to act safely on a closed
        # issue with a pinned `pr_number`. If the PR fetch raises, the
        # merge helper returns False on "could not fetch" (same return
        # value as "not merged"); flipping the issue to `rejected`
        # from the closed-issue helper anyway would permanently
        # terminal-label a merged-PR issue whose merged-path finalize
        # is just retrying through a transient network blip. The fix:
        # the closed-issue helper must defer when its own fetch
        # raises, leaving the issue alone for the next tick.
        gh = FakeGitHubClient()
        issue = make_issue(FETCH_FAILURE_ISSUE, label=LABEL_IMPLEMENTING)
        issue.closed = True
        gh.add_issue(issue)
        # Pin a `pr_number` but DON'T add the PR to `gh.pulls`. The
        # fake's `get_pr` raises `KeyError` when the number is missing,
        # which models the real PyGithub failure surface (any exception
        # from `gh.get_pr` -- transient 5xx, rate limit, network blip).
        gh.seed_state(
            FETCH_FAILURE_ISSUE,
            pr_number=FETCH_FAILURE_PR,
            branch=_issue_branch(FETCH_FAILURE_ISSUE),
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
        )

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(),
        )

        self.assertNotIn((FETCH_FAILURE_ISSUE, LABEL_REJECTED), gh.label_history)
        self.assertNotIn((FETCH_FAILURE_ISSUE, LABEL_DONE), gh.label_history)
        self.assertNotIn(
            CLOSED_WITHOUT_MERGE_AT, gh.pinned_data(FETCH_FAILURE_ISSUE),
        )
        mocks[RUN_AGENT].assert_not_called()
        mocks[CLEANUP_TERMINAL_BRANCH].assert_not_called()

    def test_merged_pr_defers(self) -> None:
        # Models the race where `_finalize_if_pr_merged` had a fetch
        # failure (returned False) but the PR is actually merged. The
        # closed-issue helper then runs its own fetch successfully,
        # sees the PR merged, and must NOT flip to `rejected` -- the
        # next tick will re-enter the merged-PR path. Otherwise a
        # merged PR's issue would be permanently mis-labeled.
        gh = FakeGitHubClient()
        issue = make_issue(MERGED_DEFER_ISSUE, label=LABEL_IMPLEMENTING)
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=MERGED_DEFER_PR,
            head_branch=_issue_branch(MERGED_DEFER_ISSUE),
            head=FakePRRef(sha=PR_HEAD_SHA),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            MERGED_DEFER_ISSUE,
            pr_number=MERGED_DEFER_PR,
            branch=_issue_branch(MERGED_DEFER_ISSUE),
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
        )
        # Force `_finalize_if_pr_merged` to bail on the merged-path
        # `set_workflow_label("done")` write by intercepting
        # `gh.get_pr`: raise on the FIRST call (the merge helper) so
        # it returns False, succeed on the SECOND call (the closed
        # helper's own fetch).
        gh.get_pr = MagicMock(  # type: ignore[assignment]
            side_effect=[
                RuntimeError("simulated transient GitHub failure"),
                pr,
            ]
        )

        mocks = self._run_implementing(
            gh, issue,
            run_agent=_agent(),
        )

        # No terminal label flip this tick: both finalize helpers
        # deferred. The next tick's `_finalize_if_pr_merged` will
        # succeed and run the proper merged-path cleanup.
        self.assertNotIn((MERGED_DEFER_ISSUE, LABEL_REJECTED), gh.label_history)
        self.assertNotIn((MERGED_DEFER_ISSUE, LABEL_DONE), gh.label_history)
        self.assertNotIn(
            CLOSED_WITHOUT_MERGE_AT, gh.pinned_data(MERGED_DEFER_ISSUE),
        )
        self.assertNotIn("merged_at", gh.pinned_data(MERGED_DEFER_ISSUE))
        mocks[RUN_AGENT].assert_not_called()
        mocks[CLEANUP_TERMINAL_BRANCH].assert_not_called()


class FinalizeIfIssueClosedUsageVerdictTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The closed-issue counterpart to `_finalize_if_pr_merged` posts the
    terminal usage receipt as a tracked comment BEFORE its single
    `write_pinned_state`, and skips it when no run was ever counted.
    Exercised directly so the receipt is pinned independently of any
    caller (the closed-issue sweep seeds no counters, so an integration
    test alone would stay green if the receipt call were removed)."""

    def test_rejected_posts_usage_verdict(self) -> None:
        from orchestrator.github import PinnedState

        gh = FakeGitHubClient()
        issue = make_issue(USAGE_ISSUE, label=LABEL_IMPLEMENTING)
        issue.closed = True
        gh.add_issue(issue)
        # No linked PR: the closed issue flips straight to `rejected`; the
        # receipt still surfaces the cumulative verdict, tracked before the
        # single write.
        seed = dict(
            issue_agent_runs=2, issue_total_tokens=USAGE_TOKEN_COUNT,
            issue_total_cost_usd=USAGE_COST_USD, issue_cost_sources=["reported"],
        )
        gh.seed_state(USAGE_ISSUE, **seed)
        state = PinnedState(comment_id=None, data=dict(seed))

        self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_issue_closed(
                    gh, _TEST_SPEC, issue, state,
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((USAGE_ISSUE, LABEL_REJECTED), gh.label_history)
        receipts = [
            body for issue_number, body in gh.posted_comments
            if issue_number == USAGE_ISSUE and body.startswith(":receipt:")
        ]
        self.assertEqual(len(receipts), 1)
        self.assertIn(
            "this issue: 2 agent runs · 3,400 tokens · $0.31", receipts[0],
        )
        receipt_comment = next(
            comment for comment in issue.comments
            if comment.body.startswith(":receipt:")
        )
        self.assertIn(
            receipt_comment.id,
            gh.pinned_data(USAGE_ISSUE).get("orchestrator_comment_ids", []),
        )

    def test_no_counters_posts_no_verdict(self) -> None:
        from orchestrator.github import PinnedState

        gh = FakeGitHubClient()
        issue = make_issue(NO_USAGE_ISSUE, label=LABEL_IMPLEMENTING)
        issue.closed = True
        gh.add_issue(issue)
        gh.seed_state(NO_USAGE_ISSUE)

        self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_issue_closed(
                    gh, _TEST_SPEC, issue, PinnedState(comment_id=None, data={}),
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((NO_USAGE_ISSUE, LABEL_REJECTED), gh.label_history)
        self.assertEqual(
            [body for issue_number, body in gh.posted_comments
             if issue_number == NO_USAGE_ISSUE and body.startswith(":receipt:")],
            [],
        )
