# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the closed-in_review label sweep and the PR combined check-state surfaces (check-runs 403 scope hint, partial-read downgrade)."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")


class GitHubClientClosedIssueSweepLabelTest(unittest.TestCase):
    """Real PyGithub's `Repository.get_issues(labels=...)` expects Label
    OBJECTS and reads `label.name`. The closed-issue sweep used to pass a
    raw string list, which raises a TypeError before the generator yields
    anything; because that exception escapes the per-issue try/except in
    `tick()`, every tick after open issues are processed would fail and
    externally-merged in_review issues would never finalize to `done`.

    This test pokes the real `GitHubClient.list_pollable_issues` against a
    mocked Repository to verify the call passes a Label object.
    """

    def test_closed_sweep_uses_label_object_from_get_label(self) -> None:
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient

        # Bypass __init__: it would require a real PAT and Github client.
        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        # __init__ normally seeds these; the closed sweep and label cache
        # both read them.
        client._pollable_calls = 0
        client._label_cache = {}
        # All get_issues calls (open sweep + per-label closed sweeps)
        # return nothing -- we only care about the call arguments.
        client.repo.get_issues.return_value = iter([])
        implementing_label = MagicMock(name="implementing_label")
        documenting_label = MagicMock(name="documenting_label")
        validating_label = MagicMock(name="validating_label")
        in_review_label = MagicMock(name="in_review_label")
        fixing_label = MagicMock(name="fixing_label")
        resolving_label = MagicMock(name="resolving_conflict_label")
        question_label = MagicMock(name="question_label")

        def fake_get_label(name: str):
            return {
                "implementing": implementing_label,
                "documenting": documenting_label,
                "validating": validating_label,
                "in_review": in_review_label,
                "fixing": fixing_label,
                "resolving_conflict": resolving_label,
                "question": question_label,
            }[name]

        client.repo.get_label.side_effect = fake_get_label

        list(client.list_pollable_issues())

        # Each sweep label is looked up by name (one query per label
        # because the GitHub Issues API treats `labels` as AND, not OR --
        # a single query for "any of these labels" is impossible).
        looked_up = [
            ca.args[0] for ca in client.repo.get_label.call_args_list
        ]
        self.assertIn("implementing", looked_up)
        self.assertIn("documenting", looked_up)
        self.assertIn("validating", looked_up)
        self.assertIn("in_review", looked_up)
        self.assertIn("fixing", looked_up)
        self.assertIn("resolving_conflict", looked_up)
        self.assertIn("question", looked_up)
        # The closed sweeps were invoked with Label OBJECTS, not strings.
        closed_calls = [
            ca for ca in client.repo.get_issues.call_args_list
            if ca.kwargs.get("state") == "closed"
        ]
        self.assertEqual(len(closed_calls), 7)
        labels_passed = [ca.kwargs["labels"] for ca in closed_calls]
        self.assertIn([implementing_label], labels_passed)
        self.assertIn([documenting_label], labels_passed)
        self.assertIn([validating_label], labels_passed)
        self.assertIn([in_review_label], labels_passed)
        self.assertIn([fixing_label], labels_passed)
        self.assertIn([resolving_label], labels_passed)
        self.assertIn([question_label], labels_passed)

    def test_missing_label_skips_closed_sweep_without_raising(self) -> None:
        # If `get_label` raises (under-scoped PAT, label not yet bootstrapped)
        # the generator must complete the open-issue sweep AND swallow the
        # closed-issue branch -- otherwise `tick()` aborts mid-loop.
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        client._pollable_calls = 0
        client._label_cache = {}
        client.repo.get_issues.return_value = iter([])
        client.repo.get_label.side_effect = GithubException(
            404, {"message": "Not Found"}, None
        )

        # Must not raise.
        out = list(client.list_pollable_issues())

        self.assertEqual(out, [])
        # Only the open sweep was invoked.
        states = [
            ca.kwargs.get("state")
            for ca in client.repo.get_issues.call_args_list
        ]
        self.assertEqual(states, ["open"])


class CheckRunsForbiddenSurfacesScopeHintTest(unittest.TestCase):
    """A 403 from the check-runs endpoint almost always means the PAT is
    missing 'Checks: read'. Silently swallowing the exception leaves
    `pr_combined_check_state` at 'none' for Actions-only PRs despite the
    PR being green. Promote the 403 to log.error with a specific message
    naming the scope.
    """

    def test_403_on_get_check_runs_logs_actionable_error(self) -> None:
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()

        commit_obj = MagicMock()
        # Combined-status path returns nothing useful (Actions-only PR).
        combined = MagicMock(state="", total_count=0)
        commit_obj.get_combined_status.return_value = combined
        # Check-runs path raises 403.
        commit_obj.get_check_runs.side_effect = GithubException(
            403, {"message": "Resource not accessible"}, None,
        )
        client.repo.get_commit.return_value = commit_obj

        pr = MagicMock()
        pr.head.sha = "deadbeef"

        with self.assertLogs("orchestrator.github", level="ERROR") as cm:
            state = client.pr_combined_check_state(pr)

        self.assertEqual(state, "none")
        joined = "\n".join(cm.output)
        self.assertIn("403", joined)
        self.assertIn("Checks: read", joined)
        self.assertIn("check_state", joined)

    def test_non_403_check_runs_failure_logs_warning_only(self) -> None:
        # 404, transient 5xx, etc. are logged at warning level and don't
        # need scope guidance. Avoid noisy ERROR for unrelated failures.
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        commit_obj = MagicMock()
        commit_obj.get_combined_status.return_value = MagicMock(
            state="", total_count=0
        )
        commit_obj.get_check_runs.side_effect = GithubException(
            500, {"message": "Internal Server Error"}, None,
        )
        client.repo.get_commit.return_value = commit_obj
        pr = MagicMock()
        pr.head.sha = "deadbeef"

        with self.assertLogs("orchestrator.github", level="WARNING") as cm:
            client.pr_combined_check_state(pr)

        # Filter to only WARNING records (assertLogs catches WARNING and above).
        warning_only = [r for r in cm.records if r.levelname == "WARNING"]
        self.assertTrue(warning_only, "should log a warning for non-403 errors")
        # No ERROR for non-403 failures.
        error_records = [r for r in cm.records if r.levelname == "ERROR"]
        self.assertEqual(error_records, [])


class PrCombinedCheckStatePartialReadFailsClosedTest(unittest.TestCase):
    """A read failure on one checks surface must NOT be masked by a
    'success' from the other surface. Otherwise a single green
    commit-status context plus failing or pending GitHub Actions check-runs
    that the PAT cannot read (403 from a missing 'Checks: read' scope, or a
    transient 5xx) would be reported as 'success' so a caller could trust
    the head as green over the unread failing checks.
    """

    def _client_with(self, *, combined_state, combined_total, check_runs_exc):
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        commit_obj = MagicMock()
        commit_obj.get_combined_status.return_value = MagicMock(
            state=combined_state, total_count=combined_total,
        )
        commit_obj.get_check_runs.side_effect = check_runs_exc
        client.repo.get_commit.return_value = commit_obj
        pr = MagicMock()
        pr.head.sha = "deadbeef"
        return client, pr

    def test_combined_success_with_check_runs_403_returns_pending(self) -> None:
        # The dangerous case: legacy commit-status says 'success' but the
        # PAT cannot read check-runs. Without the partial-read guard, a
        # caller would trust the head as green over failing/pending
        # Actions runs.
        from github import GithubException

        client, pr = self._client_with(
            combined_state="success", combined_total=1,
            check_runs_exc=GithubException(
                403, {"message": "Resource not accessible"}, None,
            ),
        )
        with self.assertLogs("orchestrator.github", level="ERROR"):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(
            state, "pending",
            "partial read with combined='success' must downgrade to "
            "'pending' so callers do not trust the head as green on half "
            "the picture",
        )

    def test_combined_success_with_check_runs_500_returns_pending(self) -> None:
        # A transient 5xx on check-runs has the same downgrade rule -- the
        # next tick may succeed and resolve to a real verdict, but until
        # then we cannot report success.
        from github import GithubException

        client, pr = self._client_with(
            combined_state="success", combined_total=1,
            check_runs_exc=GithubException(
                500, {"message": "Internal Server Error"}, None,
            ),
        )
        with self.assertLogs("orchestrator.github", level="WARNING"):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(state, "pending")

    def test_no_combined_signal_with_check_runs_403_still_returns_none(self) -> None:
        # Edge case: combined-status returned no usable signal AND
        # check-runs raised. We have NO signal at all; preserve the
        # existing 'none' return so the workflow's failed_checks branch
        # parks awaiting_human (visible to the operator) instead of
        # silently waiting forever on 'pending'.
        from github import GithubException

        client, pr = self._client_with(
            combined_state="", combined_total=0,
            check_runs_exc=GithubException(
                403, {"message": "Resource not accessible"}, None,
            ),
        )
        with self.assertLogs("orchestrator.github", level="ERROR"):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(
            state, "none",
            "no signal on either surface must keep returning 'none' so "
            "the workflow parks awaiting_human instead of pending forever",
        )
