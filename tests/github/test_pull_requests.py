# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Status helpers and client methods on the `pull_requests` owner."""
from __future__ import annotations

import unittest
from typing import Optional
from unittest.mock import MagicMock

from github import GithubException

from orchestrator.github import pull_requests as _pull_requests
from orchestrator.github.client import GitHubClient

from tests.support.github.models import FakeLabel, FakePR

_STATE_OPEN = "open"
_STATE_CLOSED = "closed"
_PR_NUMBER = 7
_HTTP_NOT_FOUND = 404
_HTTP_FORBIDDEN = 403
_HTTP_SERVER_ERROR = 500
_BRANCH = "orchestrator/issue-7"
_BASE = "main"
_OWNER_LOGIN = "geserdugarov"
_LABEL_NAME = "workflow:community_contribution"
_HEAD_SHA = "f00dcafe"
# (merged, PyGithub state) -> the one state every workflow gate reads.
_PR_STATE_CASES = (
    ((True, _STATE_OPEN), "merged"),
    ((True, _STATE_CLOSED), "merged"),
    ((False, _STATE_CLOSED), _STATE_CLOSED),
    ((False, _STATE_OPEN), _STATE_OPEN),
)

# HTTP status the ref delete raises -> whether cleanup counts the branch gone.
# A 404 means the repository's auto-delete already removed it on merge.
_BRANCH_DELETE_CASES = (
    (None, True),
    (_HTTP_NOT_FOUND, True),
    (_HTTP_FORBIDDEN, False),
)


class _RefreshingPR:
    """PR whose `mergeable` field only resolves through a `update()` refresh.

    GitHub computes mergeability asynchronously and serves `null` until the
    background job finishes, so the helper has to re-read the field after a
    refresh rather than treat the first `None` as "not mergeable".
    """

    def __init__(self, *, refresh_error: Optional[Exception] = None) -> None:
        self.mergeable: Optional[bool] = None
        self.update_calls = 0
        self._refresh_error = refresh_error

    def update(self) -> None:
        self.update_calls += 1
        if self._refresh_error is not None:
            raise self._refresh_error
        self.mergeable = True


class StatelessPrStatusTest(unittest.TestCase):
    """The status helpers read fields off a PR without any further fetch.

    A merged PR reports `merged` even though GitHub also marks it closed, and
    label matching is case-insensitive because an operator-created label can
    differ in case from the workflow's own constant.
    """

    def test_reports_merged_closed_or_open(self) -> None:
        for (merged, github_state), expected in _PR_STATE_CASES:
            with self.subTest(merged=merged, github_state=github_state):
                pull_request = FakePR(
                    number=_PR_NUMBER,
                    merged=merged,
                    state=github_state,
                )
                self.assertEqual(
                    _pull_requests.pr_state(pull_request),
                    expected,
                )

    def test_matches_label_case_insensitively(self) -> None:
        pull_request = FakePR(
            number=_PR_NUMBER,
            labels=[FakeLabel("Workflow:Community_Contribution")],
        )
        self.assertTrue(
            _pull_requests.pr_has_label(pull_request, _LABEL_NAME),
        )

    def test_absent_label_is_false(self) -> None:
        pull_request = FakePR(number=_PR_NUMBER, labels=[FakeLabel("workflow:ready")])
        self.assertFalse(
            _pull_requests.pr_has_label(pull_request, _LABEL_NAME),
        )
        self.assertFalse(
            _pull_requests.pr_has_label(
                FakePR(number=_PR_NUMBER),
                _LABEL_NAME,
            ),
        )


class PrIsMergeableTest(unittest.TestCase):
    """A pending mergeability field is refreshed once, and never guessed at."""

    def test_known_field_skips_refresh(self) -> None:
        pull_request = _RefreshingPR()
        pull_request.mergeable = False

        self.assertFalse(_pull_requests.pr_is_mergeable(pull_request))
        self.assertEqual(pull_request.update_calls, 0)

    def test_null_field_refreshes_once(self) -> None:
        pull_request = _RefreshingPR()

        self.assertTrue(_pull_requests.pr_is_mergeable(pull_request))
        self.assertEqual(pull_request.update_calls, 1)

    def test_refresh_failure_reports_unknown(self) -> None:
        # An unknown result parks the issue for the next tick; a `False` here
        # would route a mergeable PR into conflict resolution.
        pull_request = _RefreshingPR(
            refresh_error=GithubException(
                _HTTP_SERVER_ERROR,
                {"message": "Server Error"},
                None,
            ),
        )

        self.assertIsNone(_pull_requests.pr_is_mergeable(pull_request))
        self.assertEqual(pull_request.update_calls, 1)


class _PullRequestClientTestCase(unittest.TestCase):
    """Fixture handing each case a client whose repository is a mock."""

    def setUp(self) -> None:
        # Bypass the networked __init__; the methods read only `self.repo`.
        self.gh = GitHubClient.__new__(GitHubClient)
        self.gh.repo = MagicMock()
        self.gh.repo.owner.login = _OWNER_LOGIN


class PullRequestLookupTest(_PullRequestClientTestCase):
    """Lookups reach GitHub through the client's own repository."""

    def test_find_open_pr_qualifies_head_owner(self) -> None:
        # PyGithub's `head` filter matches nothing unless the branch is
        # qualified with the owning login, so an unqualified head would open a
        # duplicate PR for a branch that already has one.
        expected = MagicMock()
        self.gh.repo.get_pulls.return_value = iter([expected])

        found = self.gh.find_open_pr(branch=_BRANCH, base=_BASE)

        self.assertIs(found, expected)
        self.gh.repo.get_pulls.assert_called_once_with(
            state=_STATE_OPEN,
            head=f"{_OWNER_LOGIN}:{_BRANCH}",
            base=_BASE,
        )

    def test_missing_open_pr_is_none(self) -> None:
        self.gh.repo.get_pulls.return_value = iter([])

        self.assertIsNone(self.gh.find_open_pr(branch=_BRANCH, base=_BASE))

    def test_iter_open_prs_omits_head_and_base(self) -> None:
        # The community-contribution sweep has to see PRs opened from forks and
        # foreign branches, so this query stays unfiltered apart from state.
        listed = (MagicMock(), MagicMock())
        self.gh.repo.get_pulls.return_value = listed

        self.assertEqual(list(self.gh.iter_open_prs()), list(listed))
        self.gh.repo.get_pulls.assert_called_once_with(state=_STATE_OPEN)

    def test_get_pr_reads_by_number(self) -> None:
        fetched = self.gh.get_pr(_PR_NUMBER)

        self.gh.repo.get_pull.assert_called_once_with(_PR_NUMBER)
        self.assertIs(fetched, self.gh.repo.get_pull.return_value)


class PullRequestWriteTest(_PullRequestClientTestCase):
    """Creation, comments, labeling, merge, and branch deletion.

    Each write hands GitHub the caller's payload and reports back whether it
    landed; none of them re-reads the pull request to double-check.
    """

    def test_open_pr_sends_head_and_base(self) -> None:
        create_pull = self.gh.repo.create_pull

        opened = self.gh.open_pr(
            branch=_BRANCH,
            base=_BASE,
            title="Issue 7",
            body="closes #7",
        )

        self.assertIs(opened, create_pull.return_value)
        self.assertEqual(
            create_pull.call_args.kwargs,
            {
                "title": "Issue 7",
                "body": "closes #7",
                "head": _BRANCH,
                "base": _BASE,
            },
        )

    def test_pr_comment_posts_on_the_pr(self) -> None:
        posted = self.gh.pr_comment(_PR_NUMBER, "note")

        self.gh.repo.get_pull.assert_called_once_with(_PR_NUMBER)
        pull_request = self.gh.repo.get_pull.return_value
        pull_request.create_issue_comment.assert_called_once_with("note")
        self.assertIs(posted, pull_request.create_issue_comment.return_value)

    def test_add_pr_label_delegates_dedup(self) -> None:
        # `add_to_labels` is idempotent server-side, so the client neither
        # re-reads the PR nor filters the existing labels first.
        pull_request = MagicMock()

        self.gh.add_pr_label(pull_request, _LABEL_NAME)

        pull_request.add_to_labels.assert_called_once_with(_LABEL_NAME)
        self.gh.repo.get_pull.assert_not_called()

    def test_merge_pins_the_head_sha(self) -> None:
        # Pinning the SHA makes GitHub reject the merge if the head moved
        # since the caller read the checks and reviews it merged on.
        pull_request = MagicMock()

        self.assertTrue(self.gh.merge_pr(pull_request, sha=_HEAD_SHA))

        pull_request.merge.assert_called_once_with(
            sha=_HEAD_SHA,
            merge_method="squash",
        )

    def test_merge_failure_is_reported_not_retried(self) -> None:
        # A rejected merge (moved head, protected branch, lost race) is the
        # caller's decision to make on the next tick, not ours to retry blind.
        pull_request = MagicMock(number=_PR_NUMBER)
        pull_request.merge.side_effect = GithubException(
            _HTTP_FORBIDDEN,
            {"message": "Required status check is expected"},
            None,
        )

        merged = self.gh.merge_pr(pull_request, sha=_HEAD_SHA, method="merge")

        self.assertFalse(merged)
        pull_request.merge.assert_called_once_with(
            sha=_HEAD_SHA,
            merge_method="merge",
        )

    def test_branch_delete_reports_the_outcome(self) -> None:
        for raised_status, expected in _BRANCH_DELETE_CASES:
            with self.subTest(status=raised_status):
                self._bind_ref_delete(raised_status)
                self.assertEqual(
                    self.gh.delete_remote_branch(_BRANCH),
                    expected,
                )
                self.gh.repo.get_git_ref.assert_called_once_with(
                    f"heads/{_BRANCH}",
                )

    def _bind_ref_delete(self, raised_status: Optional[int]) -> None:
        self.gh.repo.get_git_ref.reset_mock()
        git_ref = MagicMock()
        if raised_status is not None:
            git_ref.delete.side_effect = GithubException(
                raised_status,
                {"message": "no ref"},
                None,
            )
        self.gh.repo.get_git_ref.return_value = git_ref


class PullRequestMixinOwnerTest(unittest.TestCase):
    def test_client_inherits_the_pr_mixin_owner(self) -> None:
        # The PR lookup and labeling methods reach the client through the
        # owner's mixin, so the owner class stays in the MRO.
        self.assertIn(
            _pull_requests.GitHubPullRequestMixin,
            GitHubClient.__mro__,
        )


if __name__ == "__main__":
    unittest.main()
