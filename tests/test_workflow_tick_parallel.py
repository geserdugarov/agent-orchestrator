# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Base-refresh workflow tick tests."""
from __future__ import annotations

import unittest

from unittest.mock import MagicMock, patch

from orchestrator import workflow

from tests import workflow_tick_parallel_test_support as support


class TickInvokesBaseRefreshTest(unittest.TestCase):
    """`workflow.tick` must drive `_refresh_base_and_worktrees` before any
    issue is processed -- otherwise an in-flight worktree would still be
    anchored at the base SHA from when it was first added.
    """

    def test_refresh_called_once_before_issues(self) -> None:
        gh = support.FakeGitHubClient()
        gh.add_issue(support.make_issue(1, label=support.LABEL_IMPLEMENTING))
        refresh = MagicMock()
        process = MagicMock()
        with patch.object(workflow, support.REFRESH_BASE, refresh), \
             patch.object(workflow, support.PROCESS_ISSUE, process):
            workflow.tick(gh, support._TEST_SPEC)
        refresh.assert_called_once_with(gh, support._TEST_SPEC, scheduler=None)
        process.assert_called_once()

    def test_refresh_error_does_not_block_issues(self) -> None:
        gh = support.FakeGitHubClient()
        gh.add_issue(support.make_issue(1, label=support.LABEL_IMPLEMENTING))
        refresh = MagicMock(side_effect=RuntimeError("fetch boom"))
        process = MagicMock()
        with patch.object(workflow, support.REFRESH_BASE, refresh), \
             patch.object(workflow, support.PROCESS_ISSUE, process):
            workflow.tick(gh, support._TEST_SPEC)
        process.assert_called_once()
