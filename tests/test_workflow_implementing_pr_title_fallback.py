# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing pr title fallback behavior."""

from __future__ import annotations

import unittest

from tests import implementing_pr_test_support as support

CONVENTIONAL_TITLE_ISSUE = support.CONVENTIONAL_TITLE_ISSUE
CUSTOM_PREFIX_ISSUE = support.CUSTOM_PREFIX_ISSUE
DEV_SESSION = support.DEV_SESSION
DONE_MESSAGE = support.DONE_MESSAGE
FakeGitHubClient = support.FakeGitHubClient
INFERRED_PREFIX_ISSUE = support.INFERRED_PREFIX_ISSUE
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
_ConventionalTitleFixtureMixin = support._ConventionalTitleFixtureMixin
_agent = support._agent
make_issue = support.make_issue


class ConventionalPrTitleFallbackTest(
    unittest.TestCase,
    _ConventionalTitleFixtureMixin,
):
    def test_fallback_uses_conventional_issue_title(self) -> None:
        # Issue title already conventional -> use it directly so we don't
        # produce a doubled `feat: feat: ...` form.
        gh = FakeGitHubClient()
        issue = make_issue(
            CONVENTIONAL_TITLE_ISSUE,
            label=LABEL_IMPLEMENTING,
            title="docs: clarify the README",
        )
        gh.add_issue(issue)

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=DONE_MESSAGE),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="some unconventional commit",
        )

        self.assertEqual(gh.opened_prs[0].title, "docs: clarify the README")

    def test_pr_title_preserves_custom_repo_prefix(self) -> None:
        # A repo-local prefix that is NOT a Conventional type (e.g. an
        # events repo's `event:`) must be preserved verbatim, not replaced
        # with a synthesized `feat:`.
        gh, issue = self._seeded(issue_number=CUSTOM_PREFIX_ISSUE)

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=DONE_MESSAGE),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="event: add the winter gala",
        )

        self.assertEqual(gh.opened_prs[0].title, "event: add the winter gala")

    def test_fallback_uses_inferred_repo_prefix(self) -> None:
        # First commit subject is unprefixed, so the orchestrator must
        # synthesize a title -- and it honors the repo-local prefix that
        # `_infer_subject_prefix` reads from base history instead of `feat:`.
        gh, issue = self._seeded(issue_number=INFERRED_PREFIX_ISSUE)

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=DONE_MESSAGE),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="updated the listings",
            fallback_prefix="career",
        )

        self.assertEqual(gh.opened_prs[0].title, "career: add a sparkly thing")
