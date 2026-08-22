# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Finding an issue this orchestrator created, by the marker it put in it.

The lookup a create that returned into a crash needs, and the one place a
label failure may not be answered with "no". Its caller is deciding whether an
issue it may already have opened exists, so "could not ask" read as "there is
none" is what opens a second issue for work that already has one.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from github import GithubException

from orchestrator.github.client import GitHubClient

_MARKER = "<!--orchestrator-late-child:issue=41:cycle=3:generation=1:index=0-->"

_LABEL = "workflow:blocked"

_BOT_LOGIN = "orchestrator"

_HTTP_NOT_FOUND = 404

_HTTP_FORBIDDEN = 403

_HTTP_SERVER_ERROR = 500


def _client_over(*issues) -> GitHubClient:
    """A bare client whose repository answers with these open issues."""
    client = GitHubClient.__new__(GitHubClient)
    client.repo = MagicMock()
    client._repo_slug = "owner/repo"
    client._bot_login = _BOT_LOGIN
    client._label_cache = {}
    client._absent_after_sweep = {}
    client._closed_sweeps = 0
    client.repo.get_issues.return_value = iter(issues)
    return client


def _candidate(body: str, *, login: str = _BOT_LOGIN):
    """An open issue whose body and author are what a match is decided on."""
    return MagicMock(body=body, user=MagicMock(login=login))


class MarkerLookupTest(unittest.TestCase):
    """Only an issue this orchestrator opened carrying the marker matches."""

    def test_it_returns_the_issue_carrying_the_marker(self) -> None:
        wanted = _candidate(f"a slice\n\n{_MARKER}")
        client = _client_over(_candidate("some other issue"), wanted)

        self.assertIs(
            client.find_issue_carrying(_MARKER, label=_LABEL), wanted,
        )

    def test_an_issue_nobody_here_opened_is_not_one(self) -> None:
        # The marker is an HTML comment: invisible, and trivially copied.
        client = _client_over(_candidate(_MARKER, login="outsider"))

        self.assertIsNone(client.find_issue_carrying(_MARKER, label=_LABEL))

    def test_no_match_is_none(self) -> None:
        client = _client_over(_candidate("nothing of ours"))

        self.assertIsNone(client.find_issue_carrying(_MARKER, label=_LABEL))


class MarkerLookupFailureTest(unittest.TestCase):
    """A label nobody could resolve is not the same answer as no label."""

    def test_an_absent_label_is_an_absent_child(self) -> None:
        # Creating a child is what puts the label in the repository, so a 404
        # means none was ever created and there is provably no orphan.
        client = _client_over()
        client.repo.get_label.side_effect = GithubException(
            _HTTP_NOT_FOUND, {}, None,
        )

        self.assertIsNone(client.find_issue_carrying(_MARKER, label=_LABEL))
        client.repo.get_issues.assert_not_called()

    def test_an_unreadable_label_is_raised(self) -> None:
        # The rate limit and a 5xx both arrive this way, and answering None
        # would open a second issue for a slice that already has one.
        for status in (_HTTP_FORBIDDEN, _HTTP_SERVER_ERROR):
            with self.subTest(status=status):
                client = _client_over()
                client.repo.get_label.side_effect = GithubException(
                    status, {}, None,
                )

                with self.assertRaises(GithubException):
                    client.find_issue_carrying(_MARKER, label=_LABEL)


if __name__ == "__main__":
    unittest.main()
