# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing pr titles behavior."""

from __future__ import annotations

import unittest

from tests import implementing_pr_test_support as support

BUG_FALLBACK_ISSUE = support.BUG_FALLBACK_ISSUE
CONVENTIONAL_ISSUE = support.CONVENTIONAL_ISSUE
DEV_SESSION = support.DEV_SESSION
DONE_MESSAGE = support.DONE_MESSAGE
EMPTY_SUBJECT_ISSUE = support.EMPTY_SUBJECT_ISSUE
SCOPED_CONVENTIONAL_ISSUE = support.SCOPED_CONVENTIONAL_ISSUE
SPARKLY_COMMIT_SUBJECT = support.SPARKLY_COMMIT_SUBJECT
UNCONVENTIONAL_ISSUE = support.UNCONVENTIONAL_ISSUE
_ConventionalTitleFixtureMixin = support._ConventionalTitleFixtureMixin
_agent = support._agent


class ConventionalPrTitleTest(
    unittest.TestCase,
    _ConventionalTitleFixtureMixin,
):
    """`_on_commits` derives the PR title from the agent's first commit
    subject when it already follows the Conventional-Commits convention,
    and falls back to a `<type>: <issue title>` form otherwise."""

    def test_uses_conventional_commit_subject(self) -> None:
        gh, issue = self._seeded(issue_number=CONVENTIONAL_ISSUE)

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=DONE_MESSAGE),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject=SPARKLY_COMMIT_SUBJECT,
        )

        self.assertEqual(len(gh.opened_prs), 1)
        pr = gh.opened_prs[0]
        # First-commit subject is preserved verbatim, no extra prefix.
        self.assertEqual(pr.title, SPARKLY_COMMIT_SUBJECT)
        # Traceability still in body.
        self.assertIn(f"Resolves #{issue.number}", pr.body)

    def test_uses_scoped_conventional_subject(self) -> None:
        gh, issue = self._seeded(issue_number=SCOPED_CONVENTIONAL_ISSUE)

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=DONE_MESSAGE),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            # Conventional Commits also allow `<type>(<scope>): ...` and
            # `<type>!:` for breaking changes; both must be accepted.
            first_commit_subject="fix(api)!: drop legacy endpoint",
        )

        self.assertEqual(gh.opened_prs[0].title, "fix(api)!: drop legacy endpoint")

    def test_unconventional_falls_back_to_feat(self) -> None:
        gh, issue = self._seeded(issue_number=UNCONVENTIONAL_ISSUE)

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=DONE_MESSAGE),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="updated stuff",
        )

        pr = gh.opened_prs[0]
        # Fallback uses `feat:` (no bug label) and the issue title.
        self.assertEqual(pr.title, SPARKLY_COMMIT_SUBJECT)
        self.assertIn(f"Resolves #{issue.number}", pr.body)

    def test_bug_label_falls_back_to_fix(self) -> None:
        gh, issue = self._seeded(issue_number=BUG_FALLBACK_ISSUE, label_name="bug")

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=DONE_MESSAGE),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="fixed it",
        )

        # Bug label tips the fallback to `fix:`.
        self.assertEqual(gh.opened_prs[0].title, "fix: add a sparkly thing")

    def test_pr_title_fallback_when_no_commit_subject(self) -> None:
        gh, issue = self._seeded(issue_number=EMPTY_SUBJECT_ISSUE)

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=DONE_MESSAGE),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="",
        )

        self.assertEqual(gh.opened_prs[0].title, SPARKLY_COMMIT_SUBJECT)
