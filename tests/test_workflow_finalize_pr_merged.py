# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Direct coverage of the cross-stage `_finalize_if_pr_merged` helper:
the no-`pr_number` / open-PR / closed-without-merge negative cases and
the merged-PR finalize on an open vs. already-closed issue."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

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


class FinalizeIfPrMergedTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Direct coverage of the cross-stage `_finalize_if_pr_merged` helper.

    Stages that previously had no merged-PR check (`_handle_implementing`,
    `_handle_documenting`, `_handle_validating`) plus the umbrella /
    blocked aggregation now call this helper to short-circuit a stale
    in-flight label when the linked PR was merged externally. The helper
    is the single chokepoint, so it carries its own tests in addition to
    the per-handler smoke tests.
    """

    def _state_with_pr_number(self, gh, issue_number, pr_number):
        from orchestrator.github import PinnedState
        gh.seed_state(issue_number, pr_number=pr_number)
        # Mirror what handlers do: read pinned state and hand it to the helper.
        state = PinnedState(comment_id=None, data={"pr_number": pr_number})
        return state

    def test_no_pr_number_returns_false(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(200, label="validating")
        gh.add_issue(issue)
        from orchestrator.github import PinnedState

        result = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, PinnedState()
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()

    def test_open_pr_returns_false(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(201, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=20100, head_branch="orchestrator/issue-201",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="open",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 201, 20100)

        result = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()

    def test_closed_unmerged_pr_returns_false(self) -> None:
        # Closed without merge is `rejected` territory; the helper covers
        # only the merged case so the in_review / fixing / resolving_conflict
        # handlers stay in charge of the rejected arc with their own
        # `closed_without_merge_at` stamp + `pr_closed_without_merge` event.
        gh = FakeGitHubClient()
        issue = make_issue(202, label="validating")
        gh.add_issue(issue)
        pr = FakePR(
            number=20200, head_branch="orchestrator/issue-202",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 202, 20200)

        result = self._run(
            lambda: self.assertFalse(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()

    def test_merged_pr_finalizes_open_issue(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(203, label="implementing")
        gh.add_issue(issue)
        pr = FakePR(
            number=20300, head_branch="orchestrator/issue-203",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 203, 20300)

        result = self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertIn((203, "done"), gh.label_history)
        self.assertIn("merged_at", state.data)
        self.assertTrue(issue.closed)
        result["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 203,
        )
        # An `external`-merge audit event is emitted with the
        # entry-stage label.
        kinds = [e["event"] for e in gh.recorded_events]
        self.assertIn("pr_merged", kinds)
        merged_event = next(
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        )
        self.assertEqual(merged_event.get("merge_method"), "external")
        self.assertEqual(merged_event.get("stage"), "implementing")

    def test_merged_pr_finalizes_closed_issue(self) -> None:
        # An externally-merged PR with `Resolves #N` auto-closes the issue
        # before the orchestrator can react. The helper must still
        # finalize the label (and not attempt to re-close).
        gh = FakeGitHubClient()
        issue = make_issue(204, label="validating")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=20400, head_branch="orchestrator/issue-204",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 204, 20400)

        self._run(
            lambda: self.assertTrue(
                workflow._finalize_if_pr_merged(
                    gh, _TEST_SPEC, issue, state
                )
            ),
            run_agent=_agent(),
        )
        self.assertIn((204, "done"), gh.label_history)
        self.assertTrue(issue.closed)


if __name__ == "__main__":
    unittest.main()
