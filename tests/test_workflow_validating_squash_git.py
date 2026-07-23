# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests import validating_squash_test_support as squash_support

GIT_LOG = "log"
SUBJECT_FORMAT = "--pretty=%s"
LAST_COMMIT = "-1"


class SquashHelperRealGitTest(
    squash_support.SquashGitFixtureMixin,
    unittest.TestCase,
):
    """Build conventional squash commits against a real repository."""

    def test_squash_collapses_three_commits_to_one(self) -> None:
        # First commit's subject ("fix: typo") is conventional-commit form,
        # so the squash subject reuses it. The squash message is
        # subject-only: the repo's Conventional-Commits-subject-only rule
        # forbids bodies on orchestrator-authored commits.
        squash_run = self._squash()
        self.assertTrue(
            squash_run.success,
            f"expected success, got err={squash_run.error!r}",
        )
        self.assertIsNone(squash_run.error)
        self.assertEqual(squash_run.count, 3)
        self.assertTrue(squash_run.sha)

        commits = self._commits_on_branch()
        self.assertEqual(
            len(commits),
            1,
            f"expected one commit on top of base, got {commits!r}",
        )
        # Squash subject reuses the conventional-commit first subject.
        self.assertEqual(commits[0], "fix: typo")
        # Body is empty (subject-only commit): the repo's commit-style
        # rule forbids a body or trailer on orchestrator-authored
        # commits, so the squash MUST NOT carry the legacy
        # `Squashed commits: -...` listing.
        body = squash_support.run_git(
            GIT_LOG,
            LAST_COMMIT,
            "--pretty=%B",
            cwd=self.work,
        ).strip()
        self.assertEqual(body, "fix: typo")
        self.assertNotIn("Squashed commits:", body)

    def test_issue_title_used_without_conventional(
        self,
    ) -> None:
        # Reset and rebuild the branch with non-conv-commit first subject.
        self._rebuild_topic(("typo fix", "feat: add foo"), "g")
        squash_run = self._squash(
            issue=self._make_issue(title="rename frobnicator"),
        )
        self.assertTrue(squash_run.success, squash_run.error)
        self.assertEqual(squash_run.count, 2)

        subject = squash_support.run_git(GIT_LOG, LAST_COMMIT, SUBJECT_FORMAT, cwd=self.work).strip()
        self.assertEqual(subject, "feat: rename frobnicator")

    def test_keeps_custom_prefix_first_subject(self) -> None:
        # A repo-local first-commit prefix that is NOT a Conventional type
        # (e.g. a careers site's `career:`) must be reused verbatim as the
        # squash subject -- previously it would have been discarded for a
        # synthesized `feat: <issue title>`.
        self._rebuild_topic(
            ("career: add a senior role", "fix wording"),
            "c",
        )
        squash_run = self._squash(
            issue=self._make_issue(title="hiring page"),
        )
        self.assertTrue(squash_run.success, squash_run.error)
        self.assertEqual(squash_run.count, 2)
        subject = squash_support.run_git(GIT_LOG, LAST_COMMIT, SUBJECT_FORMAT, cwd=self.work).strip()
        self.assertEqual(subject, "career: add a senior role")

    def test_infers_prefix_from_base_history(self) -> None:
        # No reusable first-commit subject, so the squash subject is
        # synthesized -- and it honors the repo-local `event:` prefix that
        # dominates recent base-branch history instead of defaulting to
        # `feat:`.
        # Seed the base branch with a history dominated by `event:`.
        self._seed_inferred_prefix_history()
        squash_run = self._squash(
            issue=self._make_issue(title="redesign the homepage"),
        )
        self.assertTrue(squash_run.success, squash_run.error)
        self.assertEqual(squash_run.count, 2)
        subject = squash_support.run_git(GIT_LOG, LAST_COMMIT, SUBJECT_FORMAT, cwd=self.work).strip()
        self.assertEqual(subject, "event: redesign the homepage")
