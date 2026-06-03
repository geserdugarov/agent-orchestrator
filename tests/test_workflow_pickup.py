# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pickup behavior for unlabeled issues: legacy decompose-off shortcut to
implementing and the `ALLOWED_ISSUE_AUTHORS` allowlist (case-insensitive
match, empty-list disables filter)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import _PatchedWorkflowMixin, _TEST_SPEC, _agent


class HandlePickupTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_pickup_with_decompose_off_routes_straight_to_implementing(
        self,
    ) -> None:
        # Legacy path retained behind the DECOMPOSE kill switch: an
        # unlabeled issue still goes straight to implementing without a
        # decomposer round, so operators can disable decomposition without
        # redeploying old binaries.
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", False):
            mocks = self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )

        self.assertTrue(
            any(":robot: orchestrator picking this up" in body
                for _, body in gh.posted_comments)
        )
        # Pickup flips the label to implementing; downstream handler may park
        # on awaiting_human but does not re-label.
        self.assertEqual(gh.label_history[0], (1, "implementing"))
        self.assertIn("created_at", gh.pinned_data(1))
        # _handle_implementing was actually entered (codex spawned).
        mocks["run_agent"].assert_called_once()

    def test_pickup_skips_issue_from_non_allowed_author(self) -> None:
        # A populated ALLOWED_ISSUE_AUTHORS allowlist must drop unlabeled
        # issues from outside that list silently -- no comment, no label,
        # no pinned state. This is the abuse guard: a stranger filing
        # issues on a public repo cannot make the orchestrator spawn agents.
        gh = FakeGitHubClient()
        issue = make_issue(1, author="stranger")
        gh.add_issue(issue)

        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("geserdugarov",)):
            mocks = self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="should not run"),
                has_new_commits=False,
            )

        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.pinned_data(1), {})
        mocks["run_agent"].assert_not_called()

    def test_pickup_proceeds_for_allowed_author(self) -> None:
        # Sanity: when the author IS in the list, pickup behaves exactly
        # like the unguarded path -- this guard is purely a triage filter.
        gh = FakeGitHubClient()
        issue = make_issue(1, author="alice")
        gh.add_issue(issue)

        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("alice", "bob")), \
             patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )

        self.assertIn((1, "implementing"), gh.label_history)
        self.assertIn("created_at", gh.pinned_data(1))

    def test_pickup_matches_author_case_insensitively(self) -> None:
        # GitHub logins are case-insensitive: "Alice" and "alice" resolve
        # to the same account. The allowlist must accept either casing on
        # both sides so a maintainer's mixed-case configuration doesn't
        # silently reject legitimate issues.
        gh = FakeGitHubClient()
        issue = make_issue(1, author="Alice")
        gh.add_issue(issue)

        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ("alice",)), \
             patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )

        self.assertIn((1, "implementing"), gh.label_history)

    def test_empty_allowlist_lets_anyone_through(self) -> None:
        # Default config: empty tuple disables the filter so existing
        # single-user setups (and any deployment that hasn't opted in)
        # keep their current "anyone can trigger" behavior.
        gh = FakeGitHubClient()
        issue = make_issue(1, author="random-user")
        gh.add_issue(issue)

        with patch.object(config, "ALLOWED_ISSUE_AUTHORS", ()), \
             patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )

        self.assertIn((1, "implementing"), gh.label_history)
