# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Superseding a pull request: one marked notice, then the close.

Its own module because the idempotency is a property of the THREAD rather than
of a payload the other pull-request writes hand GitHub: the comment and
whatever durable record a caller keeps of it cannot be made one operation, so
what stops a repeat is a hidden marker already on the pull request.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from github import GithubException

from orchestrator.github.client import GitHubClient

_STATE_OPEN = "open"
_STATE_CLOSED = "closed"
_PR_NUMBER = 7
_HTTP_FORBIDDEN = 403
_BOT_LOGIN = "orchestrator"
_MARKER = (
    "<!--orchestrator-late-supersession:issue=7:cycle=3:generation=1-->"
)
_EARLIER_MARKER = (
    "<!--orchestrator-late-supersession:issue=7:cycle=2:generation=1-->"
)
_NOTICE = f"superseded by its children\n\n{_MARKER}"
_GITHUB_LOG = "orchestrator.github"


def _unmarked_pr(*, merged: bool = False, state: str = _STATE_OPEN):
    """A pull request whose thread carries no marker of ours yet."""
    pull_request = MagicMock(number=_PR_NUMBER, merged=merged, state=state)
    pull_request.get_issue_comments.return_value = []
    return pull_request


def _bot_comment(body: str):
    """A comment this orchestrator's own token authored."""
    return MagicMock(body=body, user=MagicMock(login=_BOT_LOGIN))


class PullRequestSupersessionTest(unittest.TestCase):
    """One notice, said at most once, and then the pull request is closed."""

    def setUp(self) -> None:
        # Bypass the networked __init__; the method reads only the PR it is
        # handed and the login it authenticated as.
        self.gh = GitHubClient.__new__(GitHubClient)
        self.gh._bot_login = _BOT_LOGIN

    def test_it_says_so_and_closes_an_open_pr(self) -> None:
        pull_request = _unmarked_pr()

        superseded = self.gh.supersede_pr(
            pull_request, notice=_NOTICE, marker=_MARKER,
        )

        self.assertTrue(superseded)
        pull_request.create_issue_comment.assert_called_once_with(_NOTICE)
        pull_request.edit.assert_called_once_with(state=_STATE_CLOSED)

    def test_a_marked_thread_is_not_told_twice(self) -> None:
        # The comment and whatever record the caller keeps of it cannot be one
        # operation, so the thread rather than a receipt answers "already
        # said" -- and a retry after that crash adds nothing.
        pull_request = _unmarked_pr()
        pull_request.get_issue_comments.return_value = [
            _bot_comment(f"earlier notice\n\n{_MARKER}"),
        ]

        self.gh.supersede_pr(pull_request, notice=_NOTICE, marker=_MARKER)

        pull_request.create_issue_comment.assert_not_called()
        pull_request.edit.assert_called_once_with(state=_STATE_CLOSED)

    def test_another_episode_s_marker_is_not_ours(self) -> None:
        pull_request = _unmarked_pr()
        pull_request.get_issue_comments.return_value = [
            _bot_comment("<!--orchestrator-late-owner-recovery-->"),
        ]

        self.gh.supersede_pr(pull_request, notice=_NOTICE, marker=_MARKER)

        pull_request.create_issue_comment.assert_called_once_with(_NOTICE)

    def test_an_earlier_receipt_is_not_this_one(self) -> None:
        # A plan pull request outlives a cycle, so an unscoped marker would
        # read the previous episode's notice as this one's and say nothing.
        pull_request = _unmarked_pr()
        pull_request.get_issue_comments.return_value = [
            _bot_comment(f"superseded before\n\n{_EARLIER_MARKER}"),
        ]

        self.gh.supersede_pr(pull_request, notice=_NOTICE, marker=_MARKER)

        pull_request.create_issue_comment.assert_called_once_with(_NOTICE)

    def test_a_forged_marker_silences_nothing(self) -> None:
        # An HTML comment is invisible in the rendered thread and trivially
        # copied, so anybody could otherwise suppress the one sentence saying
        # this change is not to be merged.
        pull_request = _unmarked_pr()
        pull_request.get_issue_comments.return_value = [
            MagicMock(body=_MARKER, user=MagicMock(login="outsider")),
        ]

        self.gh.supersede_pr(pull_request, notice=_NOTICE, marker=_MARKER)

        pull_request.create_issue_comment.assert_called_once_with(_NOTICE)

    def test_a_settled_pr_is_told_not_reclosed(self) -> None:
        # A merged one must not be reopened and re-closed, and a closed one is
        # already where this wants it -- but both still owe the humans looking
        # at them a sentence saying where the work went.
        for merged, state in ((True, _STATE_OPEN), (False, _STATE_CLOSED)):
            with self.subTest(merged=merged, state=state):
                pull_request = _unmarked_pr(merged=merged, state=state)

                superseded = self.gh.supersede_pr(
                    pull_request, notice=_NOTICE, marker=_MARKER,
                )

                self.assertTrue(superseded)
                pull_request.create_issue_comment.assert_called_once_with(
                    _NOTICE,
                )
                pull_request.edit.assert_not_called()


class PullRequestSupersessionRefusalTest(unittest.TestCase):
    """What a marker nobody here wrote, and a refused call, are worth."""

    def setUp(self) -> None:
        self.gh = GitHubClient.__new__(GitHubClient)
        self.gh._bot_login = _BOT_LOGIN

    def test_a_refused_step_reports_back(self) -> None:
        # A lazy pull request raises from the first attribute read as readily
        # as from the write, and a supersession that could not be made has to
        # hand the tick back rather than end it.
        for refused in ("get_issue_comments", "create_issue_comment", "edit"):
            with self.subTest(refused=refused):
                pull_request = _unmarked_pr()
                getattr(pull_request, refused).side_effect = GithubException(
                    _HTTP_FORBIDDEN, {}, None,
                )

                with self.assertLogs(_GITHUB_LOG, level="WARNING"):
                    superseded = self.gh.supersede_pr(
                        pull_request, notice=_NOTICE, marker=_MARKER,
                    )

                self.assertFalse(superseded)


if __name__ == "__main__":
    unittest.main()
