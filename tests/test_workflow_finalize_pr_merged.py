# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Direct coverage of the cross-stage `_finalize_if_pr_merged` helper:
the no-`pr_number` / open-PR / closed-without-merge negative cases, the
merged-PR finalize on an open vs. already-closed issue, and the terminal
usage-verdict receipt it posts (tracked before the pinned-state write)."""
from __future__ import annotations

import unittest

from orchestrator import workflow
from orchestrator.github import PinnedState

from tests.fakes import (
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_helpers import (
    EVENT_PR_MERGED,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _issue_branch,
    _state_with_pr_number,
)


_VALIDATING_LABEL = "validating"
_IMPLEMENTING_LABEL = "implementing"
_STATE_CLOSED = "closed"
_PR_HEAD_SHA = "cafe1234"
_CLEANUP_MOCK_KEY = "_cleanup_terminal_branch"
_NO_PR_ISSUE_NUMBER = 200
_OPEN_PR_ISSUE_NUMBER = 201
_OPEN_PR_NUMBER = 20100
_CLOSED_PR_ISSUE_NUMBER = 202
_CLOSED_PR_NUMBER = 20200
_OPEN_ISSUE_MERGED_NUMBER = 203
_OPEN_ISSUE_MERGED_PR_NUMBER = 20300
_CLOSED_ISSUE_MERGED_NUMBER = 204
_CLOSED_ISSUE_MERGED_PR_NUMBER = 20400
_USAGE_ISSUE_NUMBER = 205
_USAGE_PR_NUMBER = 20500
_USAGE_TOTAL_TOKENS = 45200
_USAGE_TOTAL_COST = 0.87
_NO_USAGE_ISSUE_NUMBER = 206
_NO_USAGE_PR_NUMBER = 20600


def _receipt_bodies(gh: FakeGitHubClient, issue_number: int) -> list[str]:
    return [
        body
        for posted_number, body in gh.posted_comments
        if posted_number == issue_number and body.startswith(":receipt:")
    ]


def _receipt_comment(issue):
    return next(
        comment
        for comment in issue.comments
        if comment.body.startswith(":receipt:")
    )


class FinalizeIfPrMergedTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Direct coverage of the cross-stage `_finalize_if_pr_merged` helper.

    Stages that previously had no merged-PR check (`_handle_implementing`,
    `_handle_documenting`, `_handle_validating`) plus the umbrella /
    blocked aggregation now call this helper to short-circuit a stale
    in-flight label when the linked PR was merged externally. The helper
    is the single chokepoint, so it carries its own tests in addition to
    the per-handler smoke tests.
    """

    def test_no_pr_number_returns_false(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(_NO_PR_ISSUE_NUMBER, label=_VALIDATING_LABEL)
        gh.add_issue(issue)
        mocks = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, PinnedState()
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        mocks[_CLEANUP_MOCK_KEY].assert_not_called()

    def test_open_pr_returns_false(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(_OPEN_PR_ISSUE_NUMBER, label=_VALIDATING_LABEL)
        gh.add_issue(issue)
        pr = FakePR(
            number=_OPEN_PR_NUMBER,
            head_branch=_issue_branch(_OPEN_PR_ISSUE_NUMBER),
            head=FakePRRef(sha=_PR_HEAD_SHA),
            merged=False, state="open",
        )
        gh.add_pr(pr)
        state = _state_with_pr_number(
            gh,
            _OPEN_PR_ISSUE_NUMBER,
            _OPEN_PR_NUMBER,
        )

        mocks = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        mocks[_CLEANUP_MOCK_KEY].assert_not_called()

    def test_closed_unmerged_pr_returns_false(self) -> None:
        # Closed without merge is `rejected` territory; the helper covers
        # only the merged case so the in_review / fixing / resolving_conflict
        # handlers stay in charge of the rejected arc with their own
        # `closed_without_merge_at` stamp + `pr_closed_without_merge` event.
        gh = FakeGitHubClient()
        issue = make_issue(_CLOSED_PR_ISSUE_NUMBER, label=_VALIDATING_LABEL)
        gh.add_issue(issue)
        pr = FakePR(
            number=_CLOSED_PR_NUMBER,
            head_branch=_issue_branch(_CLOSED_PR_ISSUE_NUMBER),
            head=FakePRRef(sha=_PR_HEAD_SHA),
            merged=False, state=_STATE_CLOSED,
        )
        gh.add_pr(pr)
        state = _state_with_pr_number(
            gh,
            _CLOSED_PR_ISSUE_NUMBER,
            _CLOSED_PR_NUMBER,
        )

        mocks = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        mocks[_CLEANUP_MOCK_KEY].assert_not_called()


class FinalizeMergedPrTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Merged PRs finalize labels, cleanup branches, and post usage."""

    def test_merged_pr_finalizes_open_issue(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            _OPEN_ISSUE_MERGED_NUMBER,
            label=_IMPLEMENTING_LABEL,
        )
        gh.add_issue(issue)
        gh.add_pr(
            FakePR(
                number=_OPEN_ISSUE_MERGED_PR_NUMBER,
                head_branch=_issue_branch(_OPEN_ISSUE_MERGED_NUMBER),
                head=FakePRRef(sha=_PR_HEAD_SHA),
                merged=True,
                state=_STATE_CLOSED,
            ),
        )
        state = _state_with_pr_number(
            gh,
            _OPEN_ISSUE_MERGED_NUMBER,
            _OPEN_ISSUE_MERGED_PR_NUMBER,
            branch=_issue_branch(_OPEN_ISSUE_MERGED_NUMBER),
        )

        mocks = self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertIn((_OPEN_ISSUE_MERGED_NUMBER, "done"), gh.label_history)
        self.assertIn("merged_at", state.data)
        self.assertTrue(issue.closed)
        mocks[_CLEANUP_MOCK_KEY].assert_called_once_with(
            gh,
            _TEST_SPEC,
            _OPEN_ISSUE_MERGED_NUMBER,
            branch=_issue_branch(_OPEN_ISSUE_MERGED_NUMBER),
        )
        # An `external`-merge audit event is emitted with the
        # entry-stage label.
        merged_event = next(
            event for event in gh.recorded_events
            if event["event"] == EVENT_PR_MERGED
        )
        self.assertEqual(merged_event.get("merge_method"), "external")
        self.assertEqual(merged_event.get("stage"), _IMPLEMENTING_LABEL)

    def test_merged_pr_finalizes_closed_issue(self) -> None:
        # An externally-merged PR with `Resolves #N` auto-closes the issue
        # before the orchestrator can react. The helper must still
        # finalize the label (and not attempt to re-close).
        gh = FakeGitHubClient()
        issue = make_issue(
            _CLOSED_ISSUE_MERGED_NUMBER,
            label=_VALIDATING_LABEL,
        )
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=_CLOSED_ISSUE_MERGED_PR_NUMBER,
            head_branch=_issue_branch(_CLOSED_ISSUE_MERGED_NUMBER),
            head=FakePRRef(sha=_PR_HEAD_SHA),
            merged=True, state=_STATE_CLOSED,
        )
        gh.add_pr(pr)
        state = _state_with_pr_number(
            gh,
            _CLOSED_ISSUE_MERGED_NUMBER,
            _CLOSED_ISSUE_MERGED_PR_NUMBER,
        )

        self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertIn((_CLOSED_ISSUE_MERGED_NUMBER, "done"), gh.label_history)
        self.assertTrue(issue.closed)

    def test_posts_tracked_usage_verdict(self) -> None:
        # The terminal finalize surfaces the cumulative usage verdict as a
        # tracked comment posted BEFORE `write_pinned_state`, so its id is
        # persisted in `orchestrator_comment_ids` alongside the merge stamp.
        gh = FakeGitHubClient()
        issue = make_issue(_USAGE_ISSUE_NUMBER, label=_IMPLEMENTING_LABEL)
        gh.add_issue(issue)
        gh.add_pr(
            FakePR(
                number=_USAGE_PR_NUMBER,
                head_branch=_issue_branch(_USAGE_ISSUE_NUMBER),
                head=FakePRRef(sha=_PR_HEAD_SHA),
                merged=True,
                state=_STATE_CLOSED,
            ),
        )
        state = _state_with_pr_number(
            gh,
            _USAGE_ISSUE_NUMBER,
            _USAGE_PR_NUMBER,
            issue_agent_runs=3,
            issue_total_tokens=_USAGE_TOTAL_TOKENS,
            issue_total_cost_usd=_USAGE_TOTAL_COST,
            issue_cost_sources=["estimated"],
        )

        self._run(
            lambda: workflow._finalize_if_pr_merged(
                gh, _TEST_SPEC, issue, state
            ),
            run_agent=_agent(),
        )

        receipts = _receipt_bodies(gh, _USAGE_ISSUE_NUMBER)
        self.assertEqual(len(receipts), 1)
        self.assertIn(
            "this issue: 3 agent runs · 45,200 tokens · $0.87 (est.)",
            receipts[0],
        )
        # Posted before the write, so its id rode the same persisted state.
        receipt_comment = _receipt_comment(issue)
        self.assertIn(
            receipt_comment.id,
            gh.pinned_data(_USAGE_ISSUE_NUMBER).get(
                "orchestrator_comment_ids",
                [],
            ),
        )

    def test_no_counters_posts_no_verdict(self) -> None:
        # No agent ever ran against this issue (external-merge of a
        # never-worked issue): the finalize skips the zero receipt.
        gh = FakeGitHubClient()
        issue = make_issue(_NO_USAGE_ISSUE_NUMBER, label=_IMPLEMENTING_LABEL)
        gh.add_issue(issue)
        gh.add_pr(
            FakePR(
                number=_NO_USAGE_PR_NUMBER,
                head_branch=_issue_branch(_NO_USAGE_ISSUE_NUMBER),
                head=FakePRRef(sha=_PR_HEAD_SHA),
                merged=True,
                state=_STATE_CLOSED,
            ),
        )
        state = _state_with_pr_number(
            gh,
            _NO_USAGE_ISSUE_NUMBER,
            _NO_USAGE_PR_NUMBER,
        )

        self._run(
            lambda: workflow._finalize_if_pr_merged(
                gh, _TEST_SPEC, issue, state
            ),
            run_agent=_agent(),
        )

        self.assertEqual(_receipt_bodies(gh, _NO_USAGE_ISSUE_NUMBER), [])


if __name__ == "__main__":
    unittest.main()
