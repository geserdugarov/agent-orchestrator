# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.fakes import FakeGitHubClient, make_issue


class ConflictIncludedInPollableIssuesTest(unittest.TestCase):
    """An external merge can land while the orchestrator is mid-resolution:
    `Resolves #N` closes the issue, but the orchestrator must still poll
    closed-but-`resolving_conflict` issues so `_handle_resolving_conflict`'s
    terminal `pr_status == "merged"` branch can finalize to `done`.
    """

    def test_closed_conflict_issue_is_polled(self) -> None:
        gh = FakeGitHubClient()
        # Close an issue still labeled `resolving_conflict` (mirrors
        # GitHub auto-closing via `Resolves #N` after a human merge).
        issue = make_issue(900, label="resolving_conflict")
        issue.closed = True
        gh.add_issue(issue)

        polled = list(gh.list_pollable_issues())
        self.assertIn(issue, polled)

    def test_closed_in_review_issue_still_polled(self) -> None:
        # Regression: extending the sweep must NOT drop the existing
        # closed-in_review path.
        gh = FakeGitHubClient()
        issue = make_issue(901, label="in_review")
        issue.closed = True
        gh.add_issue(issue)

        polled = list(gh.list_pollable_issues())
        self.assertIn(issue, polled)

    def test_closed_unrelated_label_is_not_polled(self) -> None:
        # Closed issues with neither `in_review` nor `resolving_conflict`
        # must stay out of the sweep so it does not balloon.
        gh = FakeGitHubClient()
        issue = make_issue(902, label="done")
        issue.closed = True
        gh.add_issue(issue)

        polled = list(gh.list_pollable_issues())
        self.assertNotIn(issue, polled)


if __name__ == "__main__":
    unittest.main()
