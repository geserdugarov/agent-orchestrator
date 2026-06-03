# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The `backlog` control label is a "not yet" hold: applied to an issue
it prevents the orchestrator from decomposing, picking up, or otherwise
advancing the state machine until a human removes the label."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow
from orchestrator.github import BACKLOG_LABEL

from tests.fakes import FakeGitHubClient, FakeLabel, make_issue
from tests.workflow_helpers import _TEST_SPEC


class BacklogLabelSkipsProcessingTest(unittest.TestCase):
    """The `backlog` control label is a "not yet" hold: applied to an issue
    (typically a freshly opened one), it prevents the orchestrator from
    decomposing, picking up, or otherwise advancing the state machine until
    a human removes the label.
    """

    def test_unlabeled_issue_with_backlog_skips_pickup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(701)
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_pickup") as pickup:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        pickup.assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])

    def test_in_flight_issue_with_backlog_skips_dispatch(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(702, label="implementing")
        issue.labels.append(FakeLabel(BACKLOG_LABEL))
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_implementing") as impl:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        impl.assert_not_called()
        self.assertEqual(gh.label_history, [])

    def test_removing_backlog_allows_pickup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(703)
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_pickup") as pickup:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        pickup.assert_called_once_with(gh, _TEST_SPEC, issue)


if __name__ == "__main__":
    unittest.main()
