# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from tests.fakes import FakeGitHubClient, make_issue


class ListPollableIssuesTest(unittest.TestCase):
    """Closed-but-`in_review` issues must still be picked up so external
    manual merges (which auto-close the linked issue via "Resolves #N") get
    finalized to `done` instead of being silently dropped."""

    def test_open_only_when_no_in_review_closed(self) -> None:
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))
        gh.add_issue(make_issue(2, label="validating"))
        out = list(gh.list_pollable_issues())
        self.assertEqual({i.number for i in out}, {1, 2})

    def test_includes_closed_in_review_for_external_merge_finalization(self) -> None:
        gh = FakeGitHubClient()
        open_issue = make_issue(1, label="implementing")
        closed_in_review = make_issue(7, label="in_review")
        closed_in_review.closed = True
        # Closed but no in_review label: must be skipped (already finalized).
        closed_done = make_issue(8, label="done")
        closed_done.closed = True
        for i in (open_issue, closed_in_review, closed_done):
            gh.add_issue(i)
        out = {i.number for i in gh.list_pollable_issues()}
        self.assertEqual(out, {1, 7})

    def test_includes_closed_question_for_terminal_cleanup(self) -> None:
        # A human closing a `question`-labeled Q&A issue is the terminal
        # signal `_handle_question` consumes to finalize the issue to
        # `done` and clean up the per-issue worktree/branch. Without the
        # closed-issue sweep including `question`, the dispatcher would
        # never re-visit the closed issue and the worktree would linger.
        gh = FakeGitHubClient()
        open_issue = make_issue(1, label="implementing")
        closed_question = make_issue(9, label="question")
        closed_question.closed = True
        for i in (open_issue, closed_question):
            gh.add_issue(i)
        out = {i.number for i in gh.list_pollable_issues()}
        self.assertEqual(out, {1, 9})


class ListPollableIssuesClosedSweepTest(unittest.TestCase):
    """A closed issue stuck at `implementing` / `documenting` / `validating`
    used to be invisible to `list_pollable_issues`. The per-handler
    `_finalize_if_pr_merged` check cannot fire if the sweep does not
    yield the issue, so the sweep was extended alongside the helper.
    """

    def test_closed_implementing_is_yielded(self) -> None:
        gh = FakeGitHubClient()
        closed = make_issue(301, label="implementing")
        closed.closed = True
        gh.add_issue(closed)
        yielded = [i.number for i in gh.list_pollable_issues()]
        self.assertIn(301, yielded)

    def test_closed_documenting_is_yielded(self) -> None:
        gh = FakeGitHubClient()
        closed = make_issue(302, label="documenting")
        closed.closed = True
        gh.add_issue(closed)
        yielded = [i.number for i in gh.list_pollable_issues()]
        self.assertIn(302, yielded)

    def test_closed_validating_is_yielded(self) -> None:
        gh = FakeGitHubClient()
        closed = make_issue(303, label="validating")
        closed.closed = True
        gh.add_issue(closed)
        yielded = [i.number for i in gh.list_pollable_issues()]
        self.assertIn(303, yielded)


if __name__ == "__main__":
    unittest.main()
