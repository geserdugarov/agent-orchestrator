# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Non-PR issue filtering, issue-query options, and the closed predicate."""
from __future__ import annotations

import unittest
from datetime import datetime
from typing import Any

from orchestrator.github import issues as _issues

_STATE_OPEN = "open"
_STATE_CLOSED = "closed"
_SINCE = datetime.fromisoformat("2026-07-01T00:00:00+00:00")


def _expected_options(**overrides: Any) -> dict[str, Any]:
    """Return the newest-first open-issue options with overrides applied."""
    expected: dict[str, Any] = {
        "state": _STATE_OPEN,
        "sort": "updated",
        "direction": "desc",
    }
    expected.update(overrides)
    return expected


class _StubIssue:
    """PyGithub-shaped issue; the filter reads only these two attributes."""

    def __init__(self, number: int, *, is_pull_request: bool = False) -> None:
        self.number = number
        self.pull_request = object() if is_pull_request else None


class _StubLabel:
    def __init__(self, name: str) -> None:
        self.name = name


class _ShapedIssue:
    """An issue carrying exactly the attributes one shape of it carries.

    PyGithub hands a reader `state` and nothing called `closed`; an in-memory
    double can hand it either or both, and which attributes are absent is the
    whole point of every case below.
    """

    def __init__(self, **shape: Any) -> None:
        for name, carried in shape.items():
            setattr(self, name, carried)


class IssueIsClosedTest(unittest.TestCase):
    """Every shape a caller is handed, answered the same way.

    The real one is the one that matters: a PyGithub issue carries `state` and
    nothing called `closed`, so a reader asking for the flag alone reports
    every closed issue as open in production -- and reports it correctly
    against a double that carries the flag, which is how such a bug ships
    green. Both shapes are asserted here so no caller has to guess.
    """

    def test_a_real_issue_is_read_by_state(self) -> None:
        for state, expected in ((_STATE_CLOSED, True), (_STATE_OPEN, False)):
            with self.subTest(state=state):
                self.assertEqual(
                    _issues.issue_is_closed(_ShapedIssue(state=state)),
                    expected,
                )

    def test_a_doubles_flag_is_still_honored(self) -> None:
        for closed in (True, False):
            with self.subTest(closed=closed):
                self.assertEqual(
                    _issues.issue_is_closed(_ShapedIssue(closed=closed)),
                    closed,
                )

    def test_a_cleared_flag_falls_through_to_state(self) -> None:
        # The double carries both, and the flag being unset is not an answer:
        # `state` is what the shape is asked next.
        issue = _ShapedIssue(closed=False, state=_STATE_CLOSED)

        self.assertTrue(_issues.issue_is_closed(issue))

    def test_nothing_at_all_is_not_closed(self) -> None:
        # What a scan holds for a consumer it never fetched. Absence is the
        # caller's to interpret, and its callers fail closed on it themselves.
        self.assertFalse(_issues.issue_is_closed(None))


class IterNewNonPrIssuesTest(unittest.TestCase):
    """The poller sees each issue once and never sees a pull request.

    GitHub's issue endpoints return PRs alongside issues, and the open poll and
    the per-label closed sweep overlap, so a shared number set is what keeps a
    stage handler from running twice against the same issue in one tick.
    """

    def test_pull_requests_and_repeats_are_skipped(self) -> None:
        seen_numbers: set[int] = set()
        listed = (
            _StubIssue(1),
            _StubIssue(2, is_pull_request=True),
            _StubIssue(1),
            _StubIssue(3),
        )
        yielded = _issues.iter_new_non_pr_issues(listed, seen_numbers)
        self.assertEqual([issue.number for issue in yielded], [1, 3])
        self.assertEqual(seen_numbers, {1, 3})

    def test_numbers_from_an_earlier_query_skipped(self) -> None:
        seen_numbers = {1}
        yielded = _issues.iter_new_non_pr_issues(
            (_StubIssue(1), _StubIssue(4)),
            seen_numbers,
        )
        self.assertEqual([issue.number for issue in yielded], [4])
        self.assertEqual(seen_numbers, {1, 4})


class IssueQueryOptionsTest(unittest.TestCase):
    """Both polls request newest-first pages of the requested issue state."""

    def test_open_query_omits_label_and_since(self) -> None:
        self.assertEqual(
            _issues.issue_query_options(issue_state=_STATE_OPEN, since=None),
            _expected_options(),
        )

    def test_closed_query_carries_label_and_since(self) -> None:
        label = _StubLabel("in_review")
        self.assertEqual(
            _issues.issue_query_options(
                issue_state=_STATE_CLOSED,
                since=_SINCE,
                label=label,
            ),
            _expected_options(
                state=_STATE_CLOSED,
                labels=[label],
                since=_SINCE,
            ),
        )


if __name__ == "__main__":
    unittest.main()
