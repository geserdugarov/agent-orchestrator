# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the closed-in_review label sweep and the PR combined check-state
surfaces (check-runs 403 scope hint, partial-read downgrade)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from github import GithubException

from orchestrator.github import GitHubClient

HTTP_NOT_FOUND = 404
HTTP_FORBIDDEN = 403
HTTP_SERVER_ERROR = 500
MESSAGE_KEY = "message"
GITHUB_LOGGER = "orchestrator.github"
ERROR_LEVEL = "ERROR"
STATE_NONE = "none"
STATE_PENDING = "pending"
STATE_FAILURE = "failure"
STATE_SUCCESS = "success"


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

    def test_closed_sweep_uses_label_object(self) -> None:
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

        client.repo.get_label.side_effect = {
            "implementing": implementing_label,
            "documenting": documenting_label,
            "validating": validating_label,
            "in_review": in_review_label,
            "fixing": fixing_label,
            "resolving_conflict": resolving_label,
            "question": question_label,
        }.__getitem__

        list(client.list_pollable_issues())

        # Each sweep label is looked up by name (one query per label
        # because the GitHub Issues API treats `labels` as AND, not OR --
        # a single query for "any of these labels" is impossible).
        looked_up = [call.args[0] for call in client.repo.get_label.call_args_list]
        self.assertIn("implementing", looked_up)
        self.assertIn("documenting", looked_up)
        self.assertIn("validating", looked_up)
        self.assertIn("in_review", looked_up)
        self.assertIn("fixing", looked_up)
        self.assertIn("resolving_conflict", looked_up)
        self.assertIn("question", looked_up)
        # The closed sweeps were invoked with Label OBJECTS, not strings.
        closed_calls = [
            call
            for call in client.repo.get_issues.call_args_list
            if call.kwargs.get("state") == "closed"
        ]
        self.assertEqual(len(closed_calls), 7)
        labels_passed = [call.kwargs["labels"] for call in closed_calls]
        self.assertIn([implementing_label], labels_passed)
        self.assertIn([documenting_label], labels_passed)
        self.assertIn([validating_label], labels_passed)
        self.assertIn([in_review_label], labels_passed)
        self.assertIn([fixing_label], labels_passed)
        self.assertIn([resolving_label], labels_passed)
        self.assertIn([question_label], labels_passed)

    def test_missing_label_skips_closed_sweep(self) -> None:
        # If `get_label` raises (under-scoped PAT, label not yet bootstrapped)
        # the generator must complete the open-issue sweep AND swallow the
        # closed-issue branch -- otherwise `tick()` aborts mid-loop.
        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        client._pollable_calls = 0
        client._label_cache = {}
        client.repo.get_issues.return_value = iter([])
        client.repo.get_label.side_effect = GithubException(HTTP_NOT_FOUND, {MESSAGE_KEY: "Not Found"}, None)

        # Must not raise.
        out = list(client.list_pollable_issues())

        self.assertEqual(out, [])
        # Only the open sweep was invoked.
        states = [call.kwargs.get("state") for call in client.repo.get_issues.call_args_list]
        self.assertEqual(states, ["open"])


class CheckRunsForbiddenSurfacesScopeHintTest(unittest.TestCase):
    """A 403 from the check-runs endpoint almost always means the PAT is
    missing 'Checks: read'. Silently swallowing the exception leaves
    `pr_combined_check_state` at 'none' for Actions-only PRs despite the
    PR being green. Promote the 403 to log.error with a specific message
    naming the scope.
    """

    def test_forbidden_check_runs_log_scope_hint(self) -> None:
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
            HTTP_FORBIDDEN,
            {MESSAGE_KEY: "Resource not accessible"},
            None,
        )
        client.repo.get_commit.return_value = commit_obj

        pr = MagicMock()
        pr.head.sha = "deadbeef"

        with self.assertLogs(GITHUB_LOGGER, level=ERROR_LEVEL) as logs:
            state = client.pr_combined_check_state(pr)

        self.assertEqual(state, STATE_NONE)
        joined = "\n".join(logs.output)
        self.assertIn("403", joined)
        self.assertIn("Checks: read", joined)
        self.assertIn("check_state", joined)

    def test_other_check_error_logs_warning(self) -> None:
        # 404, transient 5xx, etc. are logged at warning level and don't
        # need scope guidance. Avoid noisy ERROR for unrelated failures.
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        commit_obj = MagicMock()
        commit_obj.get_combined_status.return_value = MagicMock(state="", total_count=0)
        commit_obj.get_check_runs.side_effect = GithubException(
            HTTP_SERVER_ERROR,
            {MESSAGE_KEY: "Internal Server Error"},
            None,
        )
        client.repo.get_commit.return_value = commit_obj
        pr = MagicMock()
        pr.head.sha = "deadbeef"

        with self.assertLogs(GITHUB_LOGGER, level="WARNING") as logs:
            client.pr_combined_check_state(pr)

        # Filter to only WARNING records (assertLogs catches WARNING and above).
        warning_only = [record for record in logs.records if record.levelname == "WARNING"]
        self.assertTrue(warning_only, "should log a warning for non-403 errors")
        # No ERROR for non-403 failures.
        error_records = [record for record in logs.records if record.levelname == ERROR_LEVEL]
        self.assertEqual(error_records, [])


class CombinedCheckStateNormalizationTest(unittest.TestCase):
    def test_normalizes_combined_statuses(self) -> None:
        from orchestrator.github import _normalize_combined_status

        cases = (
            ("", 0, None),
            (STATE_PENDING, 0, None),
            (STATE_PENDING, 1, STATE_PENDING),
            ("error", 1, STATE_FAILURE),
            (STATE_FAILURE, 1, STATE_FAILURE),
            (STATE_SUCCESS, 1, STATE_SUCCESS),
        )

        for status, total_count, expected in cases:
            with self.subTest(status=status, total_count=total_count):
                combined_status = SimpleNamespace(
                    state=status,
                    total_count=total_count,
                )
                self.assertEqual(
                    _normalize_combined_status(combined_status),
                    expected,
                )

    def test_normalizes_check_run_conclusions(self) -> None:
        from orchestrator.github import _normalize_check_runs

        cases = (
            ((), None),
            ((None, STATE_FAILURE), STATE_PENDING),
            ((STATE_FAILURE,), STATE_FAILURE),
            (("timed_out",), STATE_FAILURE),
            (("action_required",), STATE_FAILURE),
            (("cancelled",), STATE_FAILURE),
            ((STATE_SUCCESS, "neutral", "skipped"), STATE_SUCCESS),
            (("unknown",), STATE_FAILURE),
        )

        for conclusions, expected in cases:
            with self.subTest(conclusions=conclusions):
                check_runs = [SimpleNamespace(conclusion=conclusion) for conclusion in conclusions]
                self.assertEqual(_normalize_check_runs(check_runs), expected)

    def test_folds_surface_states_by_priority(self) -> None:
        from orchestrator.github import _fold_check_states

        cases = (
            ((), False, STATE_NONE),
            ((None, None), True, STATE_NONE),
            ((STATE_SUCCESS, None), True, STATE_PENDING),
            ((STATE_SUCCESS, STATE_PENDING), False, STATE_PENDING),
            ((STATE_FAILURE, STATE_PENDING), False, STATE_FAILURE),
            ((STATE_SUCCESS, STATE_SUCCESS), False, STATE_SUCCESS),
            (("unknown",), False, STATE_SUCCESS),
        )

        for states, read_failed, expected in cases:
            with self.subTest(states=states, read_failed=read_failed):
                self.assertEqual(
                    _fold_check_states(states, read_failed=read_failed),
                    expected,
                )


def _client_with(*, combined_state, combined_total, check_runs_exc):
    client = GitHubClient.__new__(GitHubClient)
    client.repo = MagicMock()
    commit_obj = MagicMock()
    commit_obj.get_combined_status.return_value = MagicMock(
        state=combined_state,
        total_count=combined_total,
    )
    commit_obj.get_check_runs.side_effect = check_runs_exc
    client.repo.get_commit.return_value = commit_obj
    pr = MagicMock()
    pr.head.sha = "deadbeef"
    return client, pr


class PartialCheckReadFailsClosedTest(unittest.TestCase):
    """A read failure on one checks surface must NOT be masked by a
    'success' from the other surface. Otherwise a single green
    commit-status context plus failing or pending GitHub Actions check-runs
    that the PAT cannot read (403 from a missing 'Checks: read' scope, or a
    transient 5xx) would be reported as 'success' so a caller could trust
    the head as green over the unread failing checks.
    """

    def test_success_plus_forbidden_returns_pending(self) -> None:
        # The dangerous case: legacy commit-status says 'success' but the
        # PAT cannot read check-runs. Without the partial-read guard, a
        # caller would trust the head as green over failing/pending
        # Actions runs.
        client, pr = _client_with(
            combined_state=STATE_SUCCESS,
            combined_total=1,
            check_runs_exc=GithubException(
                HTTP_FORBIDDEN,
                {MESSAGE_KEY: "Resource not accessible"},
                None,
            ),
        )
        with self.assertLogs(GITHUB_LOGGER, level=ERROR_LEVEL):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(
            state,
            STATE_PENDING,
            "partial read with combined='success' must downgrade to "
            "'pending' so callers do not trust the head as green on half "
            "the picture",
        )

    def test_server_error_downgrades_success(self) -> None:
        # A transient 5xx on check-runs has the same downgrade rule -- the
        # next tick may succeed and resolve to a real verdict, but until
        # then we cannot report success.
        client, pr = _client_with(
            combined_state=STATE_SUCCESS,
            combined_total=1,
            check_runs_exc=GithubException(
                HTTP_SERVER_ERROR,
                {MESSAGE_KEY: "Internal Server Error"},
                None,
            ),
        )
        with self.assertLogs(GITHUB_LOGGER, level="WARNING"):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(state, STATE_PENDING)

    def test_no_combined_plus_forbidden_returns_none(self) -> None:
        # Edge case: combined-status returned no usable signal AND
        # check-runs raised. We have NO signal at all; preserve the
        # existing 'none' return so the workflow's failed_checks branch
        # parks awaiting_human (visible to the operator) instead of
        # silently waiting forever on 'pending'.
        client, pr = _client_with(
            combined_state="",
            combined_total=0,
            check_runs_exc=GithubException(
                HTTP_FORBIDDEN,
                {MESSAGE_KEY: "Resource not accessible"},
                None,
            ),
        )
        with self.assertLogs(GITHUB_LOGGER, level=ERROR_LEVEL):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(
            state,
            STATE_NONE,
            "no signal on either surface must keep returning 'none' so "
            "the workflow parks awaiting_human instead of pending forever",
        )
