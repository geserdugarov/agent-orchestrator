# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Finding an issue this orchestrator created, by the marker it put in it.

The lookup a create that returned into a crash needs. Its caller is deciding
whether an issue it may already have opened exists, so every answer but a
definite match has to be either a definite absence or a raise: "could not ask"
read as "there is none" is what opens a second issue for work that already has
one.

Unscoped in state and in label on purpose. The window it exists for is a child
nobody has attributed yet, and a human is free to close it as junk or move its
label in that window -- which a search bounded to open issues on the label it
was born with would miss, and duplicate.
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


def _candidate(
    body: str,
    *,
    login: str = _BOT_LOGIN,
    closed: bool = False,
    label: str = _LABEL,
    is_pull: bool = False,
):
    """One candidate the walk reads: its body, its author, and its state."""
    return MagicMock(
        body=body,
        user=MagicMock(login=login),
        closed=closed,
        labels=[MagicMock(name=label)],
        pull_request=MagicMock() if is_pull else None,
    )


class MarkerLookupTest(unittest.TestCase):
    """Only an issue this orchestrator opened carrying the marker matches."""

    def test_it_returns_the_issue_carrying_the_marker(self) -> None:
        wanted = _candidate(f"a slice\n\n{_MARKER}")
        client = _client_over(_candidate("some other issue"), wanted)

        self.assertIs(
            client.find_issue_carrying(_MARKER), wanted,
        )

    def test_an_issue_nobody_here_opened_is_not_one(self) -> None:
        # The marker is an HTML comment: invisible, and trivially copied.
        client = _client_over(_candidate(_MARKER, login="outsider"))

        self.assertIsNone(client.find_issue_carrying(_MARKER))

    def test_no_match_is_none(self) -> None:
        client = _client_over(_candidate("nothing of ours"))

        self.assertIsNone(client.find_issue_carrying(_MARKER))


class MarkerLookupReachTest(unittest.TestCase):
    """Nothing about a candidate's current state hides it from the lookup."""

    def test_a_closed_child_is_still_found(self) -> None:
        # A human closing an unattributed child as junk must not have the
        # transaction open a second one beside it.
        closed = _candidate(_MARKER, closed=True)
        client = _client_over(closed)

        self.assertIs(client.find_issue_carrying(_MARKER), closed)

    def test_a_relabelled_child_is_still_found(self) -> None:
        moved = _candidate(_MARKER, label="workflow:rejected")
        client = _client_over(moved)

        self.assertIs(client.find_issue_carrying(_MARKER), moved)

    def test_a_pull_request_is_not_a_child(self) -> None:
        # The issue endpoint returns pull requests too, and a pull request
        # quoting the marker is not an issue to adopt.
        client = _client_over(_candidate(_MARKER, is_pull=True))

        self.assertIsNone(client.find_issue_carrying(_MARKER))


class MarkerLookupFailureTest(unittest.TestCase):
    """An enumeration nobody could take is not the same answer as no match."""

    def test_an_unreadable_enumeration_is_raised(self) -> None:
        # The rate limit and a 5xx both arrive this way, and answering None
        # would open a second issue for a slice that already has one.
        for status in (_HTTP_FORBIDDEN, _HTTP_SERVER_ERROR):
            with self.subTest(status=status):
                client = _client_over()
                client.repo.get_issues.side_effect = GithubException(
                    status, {}, None,
                )

                with self.assertRaises(GithubException):
                    client.find_issue_carrying(_MARKER)


if __name__ == "__main__":
    unittest.main()
