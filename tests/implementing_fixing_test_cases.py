# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed scenario records shared by implementing and fixing tests."""

from __future__ import annotations

from dataclasses import dataclass

from tests import fakes


@dataclass(frozen=True)
class IssueScenario:
    """Pair a fake client with the issue registered in it."""

    github: fakes.FakeGitHubClient
    issue: fakes.FakeIssue


def _contains_all(body: str, fragments: tuple[str, ...]) -> bool:
    normalized_body = body.lower()
    return all(fragment.lower() in normalized_body for fragment in fragments)


def posted_comment_contains(
    github: fakes.FakeGitHubClient,
    *fragments: str,
) -> bool:
    """Return whether one posted comment contains every fragment."""
    for comment_entry in github.posted_comments:
        if _contains_all(comment_entry[1], fragments):
            return True
    return False
