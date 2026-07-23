# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing pr prompts behavior."""

from __future__ import annotations

import unittest

from tests import implementing_pr_test_support as support

FEEDBACK_COMMENT_ID = support.FEEDBACK_COMMENT_ID
FOREGROUND_MARKER = support.FOREGROUND_MARKER
FakeComment = support.FakeComment
FakeUser = support.FakeUser
TEST_ISSUE_BODY = support.TEST_ISSUE_BODY
TEST_ISSUE_TITLE = support.TEST_ISSUE_TITLE
_RepoLocalStyleAssertions = support._RepoLocalStyleAssertions
_TEST_SPEC = support._TEST_SPEC
make_issue = support.make_issue
workflow = support.workflow


class RepoLocalCommitStylePromptTest(
    unittest.TestCase,
    _RepoLocalStyleAssertions,
):
    """Every commit-producing prompt teaches the agent to mirror the repo's
    OWN recent commit history (`git log`) rather than a hardcoded
    Conventional-Commits prefix list. The orchestrator runs against
    arbitrary configured repos, so a project-specific subject prefix such as
    `event:` or `career:` must be permitted by the instruction; the closed
    `feat:`/`chore:`/`refactor:`/`test:` enumeration is removed."""

    def test_implement_prompt_teaches_local_style(self) -> None:
        issue = make_issue(7, title=TEST_ISSUE_TITLE, body=TEST_ISSUE_BODY)
        self._assert_repo_local_style(
            workflow._build_implement_prompt(_TEST_SPEC, issue, comments_text="", specs=[_TEST_SPEC])
        )

    def test_fix_prompt_teaches_repo_local_style(self) -> None:
        self._assert_repo_local_style(workflow._build_fix_prompt("please fix the typo"))

    def test_followup_teaches_local_style(self) -> None:
        comments = [FakeComment(id=FEEDBACK_COMMENT_ID, body="please rename foo to bar", user=FakeUser("alice"))]
        self._assert_repo_local_style(workflow._build_pr_comment_followup(comments))

    def test_content_change_teaches_local_style(self) -> None:
        issue = make_issue(7, title=TEST_ISSUE_TITLE, body=TEST_ISSUE_BODY)
        self._assert_repo_local_style(workflow._build_user_content_change_prompt(issue, comments_text=""))

    def test_docs_prompt_does_not_require_prefix(self) -> None:
        issue = make_issue(7, title=TEST_ISSUE_TITLE, body=TEST_ISSUE_BODY)
        prompt = workflow._build_documentation_prompt(_TEST_SPEC, issue, comments_text="", specs=[_TEST_SPEC])
        # Same repo-local contract as the other commit-producing prompts.
        self._assert_repo_local_style(prompt)
        # The `docs:` type is no longer forced; a docs update may carry any
        # repo-local subject. The no-update escape hatch is preserved.
        self.assertNotIn("docs:", prompt)
        self.assertIn("DOCS: NO_CHANGE", prompt)


class ForegroundOnlyPromptTest(unittest.TestCase):
    """Every prompt that can lead to a commit must spell out the one-shot
    execution model: the session dies when the model ends its turn, so a
    backgrounded build/test ("Miri is running, I'll continue when it
    completes") is never observed and the issue parks forever."""

    def test_dev_prompts_have_foreground_note(self) -> None:
        issue = make_issue(7, title=TEST_ISSUE_TITLE, body=TEST_ISSUE_BODY)
        comments = [FakeComment(id=FEEDBACK_COMMENT_ID, body="please rename foo to bar", user=FakeUser("alice"))]
        prompts = {
            "implement": workflow._build_implement_prompt(_TEST_SPEC, issue, comments_text="", specs=[_TEST_SPEC]),
            "fix": workflow._build_fix_prompt("please fix the typo"),
            "pr_comment_followup": workflow._build_pr_comment_followup(comments),
            "documentation": workflow._build_documentation_prompt(
                _TEST_SPEC, issue, comments_text="", specs=[_TEST_SPEC]
            ),
            "conflict": workflow._build_conflict_resolution_prompt("origin/main", ["a.rs"]),
            "user_content_change": workflow._build_user_content_change_prompt(issue, comments_text=""),
        }
        for name, prompt in prompts.items():
            with self.subTest(prompt=name):
                self.assertIn(FOREGROUND_MARKER, prompt)
