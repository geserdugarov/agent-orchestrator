# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The `backlog` control label is a "not yet" hold: applied to an issue
it prevents the orchestrator from decomposing, picking up, or otherwise
advancing the state machine until a human removes the label."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import workflow
from orchestrator.github import BACKLOG_LABEL

from tests.fakes import FakeGitHubClient, FakeLabel, make_issue
from tests.workflow_helpers import _TEST_SPEC


_UNLABELED_BACKLOG_ISSUE = 701
_IN_FLIGHT_BACKLOG_ISSUE = 702
_RELEASED_BACKLOG_ISSUE = 703


class BacklogLabelSkipsProcessingTest(unittest.TestCase):
    """The `backlog` control label is a "not yet" hold: applied to an issue
    (typically a freshly opened one), it prevents the orchestrator from
    decomposing, picking up, or otherwise advancing the state machine until
    a human removes the label.
    """

    def test_unlabeled_issue_skips_pickup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(_UNLABELED_BACKLOG_ISSUE)
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        gh.add_issue(issue)

        pickup_mock = MagicMock()
        with patch.object(workflow, "_handle_pickup", pickup_mock):
            workflow._process_issue(gh, _TEST_SPEC, issue)

        pickup_mock.assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])

    def test_in_flight_issue_skips_dispatch(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(_IN_FLIGHT_BACKLOG_ISSUE, label="implementing")
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        gh.add_issue(issue)

        implementing_mock = MagicMock()
        with patch.object(workflow, "_handle_implementing", implementing_mock):
            workflow._process_issue(gh, _TEST_SPEC, issue)

        implementing_mock.assert_not_called()
        self.assertEqual(gh.label_history, [])

    def test_removing_backlog_allows_pickup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(_RELEASED_BACKLOG_ISSUE)
        gh.add_issue(issue)

        pickup_mock = MagicMock()
        with patch.object(workflow, "_handle_pickup", pickup_mock):
            workflow._process_issue(gh, _TEST_SPEC, issue)

        pickup_mock.assert_called_once_with(gh, _TEST_SPEC, issue)


if __name__ == "__main__":
    unittest.main()
