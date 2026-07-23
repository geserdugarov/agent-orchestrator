# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import patch


from github import GithubException

from orchestrator import config
from orchestrator.github import GitHubClient
from tests.fakes import FakeGitHubClient, make_issue


_IMPLEMENTING_LABEL = "implementing"
_CLOSED_IMPLEMENTING_ISSUE = 301
_CLOSED_DOCUMENTING_ISSUE = 302
_CLOSED_VALIDATING_ISSUE = 303
_FORBIDDEN_STATUS = 403


def _bare_client(repo: "_CountingRepo") -> GitHubClient:
    # Bypass the networked __init__; wire only what _cached_label touches.
    gh = GitHubClient.__new__(GitHubClient)
    gh.repo = repo
    gh._label_cache = {}
    return gh


class ListPollableIssuesTest(unittest.TestCase):
    """Closed-but-`in_review` issues must still be picked up so external
    manual merges (which auto-close the linked issue via "Resolves #N") get
    finalized to `done` instead of being silently dropped."""

    def test_open_only_when_no_in_review_closed(self) -> None:
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=_IMPLEMENTING_LABEL))
        gh.add_issue(make_issue(2, label="validating"))
        out = list(gh.list_pollable_issues())
        self.assertEqual({issue.number for issue in out}, {1, 2})

    def test_closed_review_included_for_merge_finish(self) -> None:
        open_issue = make_issue(1, label=_IMPLEMENTING_LABEL)
        closed_in_review = make_issue(7, label="in_review")
        closed_in_review.closed = True
        # Closed but no in_review label: must be skipped (already finalized).
        closed_done = make_issue(8, label="done")
        closed_done.closed = True
        gh = FakeGitHubClient((open_issue, closed_in_review, closed_done))
        out = {
            pollable_issue.number
            for pollable_issue in gh.list_pollable_issues()
        }
        self.assertEqual(out, {1, 7})

    def test_closed_question_included_for_cleanup(self) -> None:
        # A human closing a `question`-labeled Q&A issue is the terminal
        # signal `_handle_question` consumes to finalize the issue to
        # `done` and clean up the per-issue worktree/branch. Without the
        # closed-issue sweep including `question`, the dispatcher would
        # never re-visit the closed issue and the worktree would linger.
        gh = FakeGitHubClient()
        open_issue = make_issue(1, label=_IMPLEMENTING_LABEL)
        closed_question = make_issue(9, label="question")
        closed_question.closed = True
        for seeded_issue in (open_issue, closed_question):
            gh.add_issue(seeded_issue)
        out = {
            pollable_issue.number
            for pollable_issue in gh.list_pollable_issues()
        }
        self.assertEqual(out, {1, 9})


class ListPollableIssuesClosedSweepTest(unittest.TestCase):
    """A closed issue stuck at `implementing` / `documenting` / `validating`
    used to be invisible to `list_pollable_issues`. The per-handler
    `_finalize_if_pr_merged` check cannot fire if the sweep does not
    yield the issue, so the sweep was extended alongside the helper.
    """

    def test_closed_implementing_is_yielded(self) -> None:
        gh = FakeGitHubClient()
        closed = make_issue(_CLOSED_IMPLEMENTING_ISSUE, label=_IMPLEMENTING_LABEL)
        closed.closed = True
        gh.add_issue(closed)
        yielded = [issue.number for issue in gh.list_pollable_issues()]
        self.assertIn(_CLOSED_IMPLEMENTING_ISSUE, yielded)

    def test_closed_documenting_is_yielded(self) -> None:
        gh = FakeGitHubClient()
        closed = make_issue(_CLOSED_DOCUMENTING_ISSUE, label="documenting")
        closed.closed = True
        gh.add_issue(closed)
        yielded = [issue.number for issue in gh.list_pollable_issues()]
        self.assertIn(_CLOSED_DOCUMENTING_ISSUE, yielded)

    def test_closed_validating_is_yielded(self) -> None:
        gh = FakeGitHubClient()
        closed = make_issue(_CLOSED_VALIDATING_ISSUE, label="validating")
        closed.closed = True
        gh.add_issue(closed)
        yielded = [issue.number for issue in gh.list_pollable_issues()]
        self.assertIn(_CLOSED_VALIDATING_ISSUE, yielded)


class ClosedSweepCadenceTest(unittest.TestCase):
    """`CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` batches the per-label closed-issue
    recovery sweep so its fixed request cost is not paid every tick. The
    open-issue poll must stay every tick; only the closed sweep is throttled.
    """

    def test_default_runs_closed_sweep_every_tick(self) -> None:
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=_IMPLEMENTING_LABEL))
        closed = make_issue(7, label="in_review")
        closed.closed = True
        gh.add_issue(closed)
        # Default knob (1): the closed issue surfaces on every call.
        for _ in range(3):
            out = {issue.number for issue in gh.list_pollable_issues()}
            self.assertEqual(out, {1, 7})

    def test_sweep_runs_first_then_every_nth_call(self) -> None:
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=_IMPLEMENTING_LABEL))
        closed = make_issue(7, label="in_review")
        closed.closed = True
        gh.add_issue(closed)
        with patch.object(config, "CLOSED_ISSUE_SWEEP_EVERY_N_TICKS", 3):
            # Call 1 (first): sweep runs -> closed issue present.
            self.assertEqual({issue.number for issue in gh.list_pollable_issues()}, {1, 7})
            # Calls 2 and 3: sweep skipped -> open issue only.
            self.assertEqual({issue.number for issue in gh.list_pollable_issues()}, {1})
            self.assertEqual({issue.number for issue in gh.list_pollable_issues()}, {1})
            # Call 4 (== first + N): sweep runs again.
            self.assertEqual({issue.number for issue in gh.list_pollable_issues()}, {1, 7})

    def test_throttle_never_drops_open_issues(self) -> None:
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label=_IMPLEMENTING_LABEL))
        gh.add_issue(make_issue(2, label="validating"))
        with patch.object(config, "CLOSED_ISSUE_SWEEP_EVERY_N_TICKS", 5):
            for _ in range(5):
                out = {issue.number for issue in gh.list_pollable_issues()}
                self.assertEqual(out, {1, 2})


class _StubLabel:
    def __init__(self, name: str) -> None:
        self.name = name


class _CountingRepo:
    """Minimal stand-in for PyGithub's Repository that records how many times
    `get_label` is called, so the cache can be asserted without network."""

    def __init__(self, *, missing: set[str] | None = None) -> None:
        self.get_label_calls: list[str] = []
        self._missing = missing or set()

    def get_label(self, name: str):
        self.get_label_calls.append(name)
        if name in self._missing:
            raise GithubException(
                _FORBIDDEN_STATUS,
                {"message": "Forbidden"},
                None,
            )
        return _StubLabel(name)


class CachedLabelTest(unittest.TestCase):
    """`_cached_label` must fetch each workflow label at most once per client
    (labels are immutable after `ensure_workflow_labels`), while still
    retrying a failed lookup every call so a fixed PAT / created label is
    picked up without a restart.
    """

    def test_resolved_label_is_fetched_once(self) -> None:
        repo = _CountingRepo()
        gh = _bare_client(repo)
        for _ in range(5):
            label = gh._cached_label(_IMPLEMENTING_LABEL)
            self.assertEqual(label.name, _IMPLEMENTING_LABEL)
        self.assertEqual(repo.get_label_calls, [_IMPLEMENTING_LABEL])

    def test_failed_lookup_is_not_cached_and_retries(self) -> None:
        repo = _CountingRepo(missing={_IMPLEMENTING_LABEL})
        gh = _bare_client(repo)
        self.assertIsNone(gh._cached_label(_IMPLEMENTING_LABEL))
        self.assertIsNone(gh._cached_label(_IMPLEMENTING_LABEL))
        # Both calls hit GitHub: a transient 403 must not poison the cache.
        self.assertEqual(
            repo.get_label_calls,
            [_IMPLEMENTING_LABEL, _IMPLEMENTING_LABEL],
        )


if __name__ == "__main__":
    unittest.main()
