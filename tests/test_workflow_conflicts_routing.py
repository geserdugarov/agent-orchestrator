# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import workflow

from tests.fakes import FakeGitHubClient, make_issue
from tests.workflow_helpers import _TEST_SPEC

CONFLICT_ISSUE = 42


class HandleResolvingConflictDispatchTest(unittest.TestCase):
    """The dispatcher must route `resolving_conflict` to the dedicated
    handler -- this is a label-rollout regression check that survives
    the placeholder being replaced by the real implementation."""

    def test_dispatcher_routes_conflict_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(CONFLICT_ISSUE, label="resolving_conflict")
        gh.add_issue(issue)

        conflict_handler = MagicMock()
        with patch.object(workflow, "_handle_resolving_conflict", conflict_handler):
            workflow._process_issue(gh, _TEST_SPEC, issue)

        conflict_handler.assert_called_once_with(gh, _TEST_SPEC, issue)


if __name__ == "__main__":
    unittest.main()
